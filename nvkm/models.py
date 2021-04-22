import logging
import pickle
from functools import partial
from typing import Callable, List, Tuple, Union

import jax.experimental.optimizers as opt
import jax.numpy as jnp
import jax.random as jrnd
import jax.scipy as jsp
import matplotlib.pyplot as plt
from jax import jit, value_and_grad, vmap
from jax.config import config
from jax.experimental.host_callback import id_print
from jax.ops import index, index_add

from .integrals import fast_I
from .settings import JITTER
from .utils import choleskyize, eq_kernel, l2p, map2matrix, vmap_scan
from .vi import (
    IndependentGaussians,
    MOIndependentGaussians,
    VIPars,
    gaussain_likelihood,
)

config.update("jax_enable_x64", True)


class EQApproxGP:
    def __init__(
        self,
        z: Union[jnp.DeviceArray, None] = None,
        v: Union[jnp.DeviceArray, None] = None,
        N_basis: int = 500,
        D: int = 1,
        ls: float = 1.0,
        amp: float = 1.0,
        noise: float = 0.0,
    ):

        self.z = z
        self.v = v
        self.N_basis = N_basis
        self.D = D

        self.ls = ls
        self.pr = l2p(ls)
        self.amp = amp
        self.noise = noise

        self.Kvv = None
        self.LKvv = None

        if self.z == None:
            pass

        else:
            try:
                assert self.z.shape[1] == self.D

            except IndexError:
                self.z = self.z.reshape(-1, 1)
                assert self.D == 1

            except AssertionError:
                raise ValueError(
                    "Dimension of inducing points does not match dimension of GP."
                )

            self.Kvv, self.LKvv = self.compute_covariances(amp, ls)

    @partial(jit, static_argnums=(0,))
    def compute_covariances(self, amp, ls):
        Kvv = map2matrix(self.kernel, self.z, self.z, amp, ls) + (
            self.noise + JITTER
        ) * jnp.eye(self.z.shape[0])
        LKvv = jnp.linalg.cholesky(Kvv)
        return Kvv, LKvv

    @partial(jit, static_argnums=(0,))
    def kernel(self, t, tp, amp, ls):
        return eq_kernel(t, tp, amp, ls)

    @partial(jit, static_argnums=(0, 2))
    def sample_thetas(self, key, shape, ls):
        # FT of isotropic gaussain is inverse varience
        return jrnd.normal(key, shape) / ls

    @partial(jit, static_argnums=(0, 2))
    def sample_betas(self, key, shape):
        return jrnd.uniform(key, shape, maxval=2 * jnp.pi)

    @partial(jit, static_argnums=(0, 2))
    def sample_ws(self, key, shape, amp):
        return amp * jnp.sqrt(2 / self.N_basis) * jrnd.normal(key, shape)

    @partial(jit, static_argnums=(0,))
    def phi(self, t, theta, beta):
        return jnp.cos(jnp.dot(theta, t) + beta)

    @partial(jit, static_argnums=(0,))
    def compute_Phi(self, thetas, betas):
        return vmap(lambda zi: self.phi(zi, thetas, betas))(self.z)

    @partial(jit, static_argnums=(0,))
    def compute_q(self, v, LKvv, thetas, betas, ws):
        Phi = self.compute_Phi(thetas, betas)
        b = v - Phi @ ws
        return jsp.linalg.cho_solve((LKvv, True), b)

    @partial(jit, static_argnums=(0, 4))
    def _sample(self, t, v, amp, Ns, key=jrnd.PRNGKey(1)):

        try:
            assert t.shape[1] == self.D

        except IndexError:
            t = t.reshape(-1, 1)
            assert self.D == 1

        except AssertionError:
            raise ValueError("Dimension of input does not match dimension of GP.")
        # sample random parameters
        skey = jrnd.split(key, 3)
        thetas = self.sample_thetas(skey[0], (Ns, self.N_basis, self.D), self.ls)
        betas = self.sample_betas(skey[1], (Ns, self.N_basis))
        ws = self.sample_ws(skey[2], (Ns, self.N_basis), amp)

        # fourier basis part
        samps = vmap(
            lambda ti: vmap(lambda thi, bi, wi: jnp.dot(wi, self.phi(ti, thi, bi)))(
                thetas, betas, ws
            )
        )(t)

        # canonical basis part
        if v is None:
            pass
        else:
            qs = vmap(lambda thi, bi, wi: self.compute_q(v, self.LKvv, thi, bi, wi))(
                thetas, betas, ws
            )  # Ns x Nz
            kv = map2matrix(self.kernel, t, self.z, amp, self.ls)  # Nt x Nz
            kv = jnp.einsum("ij, kj", qs, kv)  # Nt x Ns

            samps += kv.T

        return samps

    def sample(self, t, Ns=100, key=jrnd.PRNGKey(1)):

        return self._sample(t, self.v, self.amp, Ns, key=key)


class NVKM:
    def __init__(
        self,
        zgs: Union[List[jnp.DeviceArray], None] = [None],
        vgs: Union[List[jnp.DeviceArray], None] = [None],
        zu: Union[jnp.DeviceArray, None] = None,
        vu: Union[jnp.DeviceArray, None] = None,
        N_basis: int = 500,
        C: int = 1,
        noise: float = 0.5,
        alpha: float = 1.0,
        lsgs: List[float] = [1.0],
        ampgs: List[float] = [1.0],
        lsu: float = 1.0,
        ampu: float = 1.0,
    ):

        self.N_basis = N_basis
        self.C = C
        self.noise = noise
        self.alpha = alpha

        self.lsgs = lsgs
        self.lsu = lsu
        self.ampgs = ampgs
        self.ampu = ampu

        self.zgs = zgs
        self.zu = zu
        self.vgs = vgs
        self.vu = vu

        self.g_gps = self.set_G_gps(ampgs, lsgs)
        self.u_gp = self.set_u_gp(ampu, lsu)

        if vu is None:
            self.vu = self.u_gp.sample(zu, 1).flatten()
            self.u_gp = self.set_u_gp(ampu, lsu)

        for i in range(C):
            if vgs[i] is None:
                self.vgs[i] = self.g_gps[i].sample(zgs[i], 1).flatten()
        self.g_gps = self.set_G_gps(ampgs, lsgs)

    def set_G_gps(self, ampgs, lsgs):
        gps = [
            EQApproxGP(
                z=self.zgs[i],
                v=self.vgs[i],
                N_basis=self.N_basis,
                D=i + 1,
                ls=lsgs[i],
                amp=ampgs[i],
            )
            for i in range(self.C)
        ]
        return gps

    def set_u_gp(self, ampu, lsu):
        return EQApproxGP(
            z=self.zu, v=self.vu, N_basis=self.N_basis, D=1, ls=lsu, amp=ampu
        )

    def _sample(self, t, vgs, vu, ampgs, N_s=10, key=jrnd.PRNGKey(1)):

        samps = jnp.zeros((len(t), N_s))
        skey = jrnd.split(key, 4)

        u_gp = self.u_gp
        thetaul = u_gp.sample_thetas(skey[0], (N_s, u_gp.N_basis, 1), u_gp.ls)
        betaul = u_gp.sample_betas(skey[1], (N_s, u_gp.N_basis))
        wul = u_gp.sample_ws(skey[2], (N_s, u_gp.N_basis), u_gp.amp)

        qul = vmap(lambda thi, bi, wi: u_gp.compute_q(vu, u_gp.LKvv, thi, bi, wi))(
            thetaul, betaul, wul
        )

        for i in range(0, self.C):
            skey = jrnd.split(skey[3], 4)

            G_gp_i = self.g_gps[i]
            thetagl = G_gp_i.sample_thetas(
                skey[0], (N_s, G_gp_i.N_basis, G_gp_i.D), G_gp_i.ls
            )
            betagl = G_gp_i.sample_betas(skey[1], (N_s, G_gp_i.N_basis))
            wgl = G_gp_i.sample_ws(skey[2], (N_s, G_gp_i.N_basis), G_gp_i.amp)
            _, G_LKvv = G_gp_i.compute_covariances(ampgs[i], G_gp_i.ls)
            qgl = vmap(
                lambda thi, bi, wi: G_gp_i.compute_q(vgs[i], G_LKvv, thi, bi, wi)
            )(thetagl, betagl, wgl)

            samps += vmap_scan(
                lambda ti: vmap(
                    lambda thetags, betags, thetaus, betaus, wgs, qgs, wus, qus: fast_I(
                        ti,
                        G_gp_i.z,
                        u_gp.z,
                        thetags,
                        betags,
                        thetaus,
                        betaus,
                        wgs,
                        qgs,
                        wus,
                        qus,
                        ampgs[i],
                        sigu=u_gp.amp,
                        alpha=self.alpha,
                        pg=G_gp_i.pr,
                        pu=u_gp.pr,
                    )
                )(thetagl, betagl, thetaul, betaul, wgl, qgl, wul, qul,),
                t,
            )

        return samps

    def sample(self, t, N_s=10, key=jrnd.PRNGKey(1)):
        return self._sample(t, self.vgs, self.vu, self.ampgs, N_s=N_s, key=key)

    def plot_samples(self, t, N_s, save=False, key=jrnd.PRNGKey(1)):
        skey = jrnd.split(key, 2)

        _, axs = plt.subplots(2, 1, figsize=(10, 7))
        samps = self.sample(t, N_s, key=skey[0])
        axs[0].plot(t, samps, c="green", alpha=0.5)
        axs[0].legend()

        u_samps = self.u_gp.sample(t, N_s, key=skey[1])

        axs[1].plot(t, u_samps, c="blue", alpha=0.5)
        axs[1].scatter(
            self.u_gp.z, self.u_gp.v, label="Inducing Points", marker="x", c="green",
        )
        axs[1].legend()
        if save:
            plt.savefig(save)
        plt.show()


class MOVarNVKM:
    def __init__(
        self,
        zgs: List[List[jnp.DeviceArray]],
        zu: jnp.DeviceArray,
        data: Tuple[List[jnp.DeviceArray]],
        q_class: MOIndependentGaussians = MOIndependentGaussians,
        q_pars_init: Union[VIPars, None] = None,
        q_initializer_pars=None,
        likelihood: Callable = gaussain_likelihood,
        N_basis: int = 500,
        ampgs: List[List[float]] = [[1.0], [1.0]],
        noise: List[float] = [1.0, 1.0],
        alpha: List[float] = [1.0, 1.0],
        lsgs: List[List[float]] = [[1.0], [1.0]],
        lsu: float = 1.0,
        ampu: float = 1.0,
    ):

        self.zgs = zgs
        self.vgs = None
        self.zu = zu
        self.vu = None

        self.N_basis = N_basis
        self.C = [len(l) for l in zgs]
        self.O = len(zgs)
        self.noise = noise
        self.alpha = alpha

        self.lsgs = lsgs
        self.lsu = lsu
        self.ampgs = ampgs
        self.ampu = ampu

        self.g_gps = self.set_G_gps(ampgs, lsgs)
        self.u_gp = self.set_u_gp(ampu, lsu)

        self.p_pars = self._compute_p_pars(self.ampgs, self.lsgs, self.ampu, self.lsu)
        self.data = data
        self.likelihood = likelihood
        self.q_of_v = q_class()
        if q_pars_init is None:
            q_pars_init = self.q_of_v.initialize(self, q_initializer_pars)
        self.q_pars = q_pars_init

    def set_G_gps(self, ampgs, lsgs):
        return [
            [
                EQApproxGP(
                    z=self.zgs[i][j],
                    v=None,
                    N_basis=self.N_basis,
                    D=j + 1,
                    ls=lsgs[i][j],
                    amp=ampgs[i][j],
                )
                for j in range(self.C[i])
            ]
            for i in range(self.O)
        ]

    def set_u_gp(self, ampu, lsu):
        return EQApproxGP(
            z=self.zu, v=None, N_basis=self.N_basis, D=1, ls=lsu, amp=ampu
        )

    @partial(jit, static_argnums=(0,))
    def _compute_p_pars(self, ampgs, lsgs, ampu, lsu):
        return {
            "LK_gs": [
                [
                    self.g_gps[i][j].compute_covariances(ampgs[i][j], lsgs[i][j])[1]
                    for j in range(self.C[i])
                ]
                for i in range(self.O)
            ],
            "LK_u": self.u_gp.compute_covariances(ampu, lsu)[1],
        }

    def sample_diag_g_gps(self, ts, N_s, key=jrnd.PRNGKey(1)):
        key = jrnd.split(key)
        v_gs = self.q_of_v.sample(self.q_pars, N_s, key[1])["gs"]
        samps = []
        for i in range(self.O):
            il = []
            for j, gp in enumerate(self.g_gps[i]):
                key = jrnd.split(key[0], N_s + 1)
                il.append(
                    vmap(
                        lambda vi, keyi: gp._sample(
                            ts[i][j], vi, gp.amp, 1, keyi
                        ).flatten()
                    )(v_gs[i][j], key[1:]).T
                )
            samps.append(il)

        return samps

    def sample_u_gp(self, t, N_s, key=jrnd.PRNGKey(1)):
        skey = jrnd.split(key, N_s + 1)

        v_u = self.q_of_v.sample(self.q_pars, N_s, skey[0])["u"]

        return vmap(
            lambda vi, keyi: self.u_gp._sample(
                t, vi, self.u_gp.amp, 1, key=keyi
            ).flatten()
        )(v_u, skey[1:]).T

    @partial(jit, static_argnums=(0, 7))
    def _sample(self, ts, q_pars, ampgs, lsgs, ampu, lsu, N_s, key):

        skey = jrnd.split(key, 5)
        v_samps = self.q_of_v.sample(q_pars, N_s, skey[4])

        u_gp = self.u_gp
        thetaul = u_gp.sample_thetas(skey[0], (N_s, u_gp.N_basis, 1), lsu)
        betaul = u_gp.sample_betas(skey[1], (N_s, u_gp.N_basis))
        wul = u_gp.sample_ws(skey[2], (N_s, u_gp.N_basis), ampu)

        _, u_LKvv = u_gp.compute_covariances(ampu, lsu)

        qul = vmap(lambda vui, thi, bi, wi: u_gp.compute_q(vui, u_LKvv, thi, bi, wi))(
            v_samps["u"], thetaul, betaul, wul
        )

        samps = []
        for i in range(self.O):
            sampsi = jnp.zeros((len(ts[i]), N_s))
            for j in range(self.C[i]):
                skey = jrnd.split(skey[3], 4)

                G_gp_i = self.g_gps[i][j]

                thetagl = G_gp_i.sample_thetas(
                    skey[0], (N_s, G_gp_i.N_basis, G_gp_i.D), lsgs[i][j]
                )
                betagl = G_gp_i.sample_betas(skey[1], (N_s, G_gp_i.N_basis))
                wgl = G_gp_i.sample_ws(skey[2], (N_s, G_gp_i.N_basis), ampgs[i][j])
                _, G_LKvv = G_gp_i.compute_covariances(ampgs[i][j], lsgs[i][j])

                qgl = vmap(
                    lambda vgi, thi, bi, wi: G_gp_i.compute_q(vgi, G_LKvv, thi, bi, wi)
                )(v_samps["gs"][i][j], thetagl, betagl, wgl)
                # samps += jnp.zeros((len(t), N_s))
                sampsi += vmap_scan(
                    lambda ti: vmap(
                        lambda thetags, betags, thetaus, betaus, wgs, qgs, wus, qus: fast_I(
                            ti,
                            G_gp_i.z,
                            u_gp.z,
                            thetags,
                            betags,
                            thetaus,
                            betaus,
                            wgs,
                            qgs,
                            wus,
                            qus,
                            ampgs[i][j],
                            ampu,
                            self.alpha[i],
                            l2p(lsgs[i][j]),
                            l2p(lsgs[i][j]),
                        )
                    )(thetagl, betagl, thetaul, betaul, wgl, qgl, wul, qul,),
                    ts[i],
                )
            samps.append(sampsi)
        return samps

    def sample(self, ts, N_s, key=jrnd.PRNGKey(1)):
        return self._sample(
            ts, self.q_pars, self.ampgs, self.lsgs, self.ampu, self.lsu, N_s, key
        )

    @partial(jit, static_argnums=(0, 8))
    def _compute_bound(self, data, q_pars, ampgs, lsgs, ampu, lsu, noise, N_s, key):
        p_pars = self._compute_p_pars(ampgs, self.lsgs, self.ampu, self.lsu)

        for i in range(self.O):
            for j in range(self.C[i]):
                q_pars["LC_gs"][i][j] = choleskyize(q_pars["LC_gs"][i][j])
        q_pars["LC_u"] = choleskyize(q_pars["LC_u"])

        KL = self.q_of_v.KL(p_pars, q_pars)

        xs, ys = data
        samples = self._sample(xs, q_pars, ampgs, lsgs, ampu, lsu, N_s, key)
        like = 0.0
        for i in range(self.O):
            like += self.likelihood(ys[i], samples[i], noise[i])
        return -(KL + like)

    def compute_bound(self, N_s, key=jrnd.PRNGKey(1)):
        return self._compute_bound(
            self.data,
            self.q_pars,
            self.ampgs,
            self.lsgs,
            self.ampu,
            self.lsu,
            self.noise,
            N_s,
            key,
        )

    def fit(self, its, lr, batch_size, N_s, dont_fit=[], key=jrnd.PRNGKey(1)):

        xs, ys = self.data

        opt_init, opt_update, get_params = opt.adam(lr)

        dpars = {
            "q_pars": self.q_pars,
            "ampgs": self.ampgs,
            "lsgs": self.lsgs,
            "ampu": self.ampu,
            "lsu": self.lsu,
            "noise": self.noise,
        }
        opt_state = opt_init(dpars)

        for i in range(its):
            skey, key = jrnd.split(key, 2)
            y_bs = []
            x_bs = []
            for j in range(self.O):
                skey, key = jrnd.split(key, 2)

                if batch_size:
                    rnd_idx = jrnd.choice(key, len(ys[j]), shape=(batch_size,))
                    y_bs.append(ys[j][rnd_idx])
                    x_bs.append(xs[j][rnd_idx])
                else:
                    y_bs.append(ys[j])
                    x_bs.append(xs[j])

            value, grads = value_and_grad(
                lambda dp: self._compute_bound(
                    (x_bs, y_bs),
                    dp["q_pars"],
                    dp["ampgs"],
                    dp["lsgs"],
                    dp["ampu"],
                    dp["lsu"],
                    dp["noise"],
                    N_s,
                    skey,
                )
            )(dpars)
            opt_state = opt_update(i, grads, opt_state)

            for k in dpars.keys():
                if k not in dont_fit:
                    dpars[k] = get_params(opt_state)[k]

            if jnp.any(jnp.isnan(value)):
                print("nan F!!")
                return dpars

            elif i % 10 == 0:
                print(f"it: {i} F: {value} ")

        for k in dpars.keys():
            setattr(self, k, dpars[k])

        self.p_pars = self._compute_p_pars(self.ampgs, self.lsgs, self.ampu, self.lsu)
        self.g_gps = self.set_G_gps(self.ampgs, self.lsgs)
        self.u_gp = self.set_u_gp(self.ampu, self.lsu)

    def plot_samples(self, tu, tys, N_s, save=False, key=jrnd.PRNGKey(304)):

        _, axs = plt.subplots(self.O + 1, 1, figsize=(10, 3.5 * (1 + self.O)))

        u_samps = self.sample_u_gp(tu, N_s, key=key)
        axs[0].set_ylabel(f"$u$")
        axs[0].set_xlabel("$t$")
        axs[0].scatter(self.zu, self.q_pars["mu_u"], c="blue", alpha=0.5)
        axs[0].plot(tu, u_samps, c="blue", alpha=0.5)

        samps = self.sample(tys, N_s, key=key)
        for i in range(0, self.O):
            axs[i + 1].set_ylabel(f"$y_{i+1}$")
            axs[i + 1].set_xlabel("$t$")
            axs[i + 1].plot(tys[i], samps[i], c="green", alpha=0.5)
            axs[i + 1].scatter(self.data[0][i], self.data[1][i])

        if save:
            plt.savefig(save)
        plt.show()

    def plot_filters(self, tf, N_s, save=False, key=jrnd.PRNGKey(211)):
        tfs = [
            [jnp.vstack((tf for j in range(gp.D))).T for gp in self.g_gps[i]]
            for i in range(self.O)
        ]
        g_samps = self.sample_diag_g_gps(tfs, 10)

        _, axs = plt.subplots(
            max(self.C), self.O, figsize=(4 * self.O, 2 * max(self.C)),
        )
        for i in range(self.O):
            for j in range(self.C[i]):
                y = g_samps[i][j].T * jnp.exp(-self.alpha[i] * (tf) ** 2)
                axs[j][i].plot(tf, y.T, c="red", alpha=0.5)
                axs[j][i].set_title("$G_{%s, %s}$" % (i + 1, j + 1))
            for k in range(self.C[i], max(self.C)):
                axs[k][i].axis("off")
        plt.tight_layout()
        if save:
            plt.savefig(save)
        plt.show()


class VariationalNVKM(MOVarNVKM):
    def __init__(
        self,
        zgs: List[jnp.DeviceArray],
        zu: jnp.DeviceArray,
        data: Tuple[jnp.DeviceArray, jnp.DeviceArray],
        ampgs: List[float] = [1.0],
        lsgs: List[float] = [1.0],
        noise: float = 0.5,
        alpha: float = 1.0,
        q_pars_init: Union[VIPars, None] = None,
        **kwargs,
    ):
        if q_pars_init is not None:
            q_pars_init["mu_gs"] = [q_pars_init["mu_gs"]]
            q_pars_init["LC_gs"] = [q_pars_init["LC_gs"]]

        super().__init__(
            [zgs],
            zu,
            ([data[0]], [data[1]]),
            ampgs=[ampgs],
            lsgs=[lsgs],
            noise=[noise],
            alpha=[alpha],
            q_pars_init=q_pars_init,
            **kwargs,
        )

    def sample(self, t, N_s, key=jrnd.PRNGKey(1)):
        if not isinstance(t, list):
            return self._sample(
                [t], self.q_pars, self.ampgs, self.lsgs, self.ampu, self.lsu, N_s, key
            )[0]
        else:
            return self._sample(
                t, self.q_pars, self.ampgs, self.lsgs, self.ampu, self.lsu, N_s, key
            )

    def plot_samples(self, t, N_s, save=False, key=jrnd.PRNGKey(13)):
        super().plot_samples(t, [t], N_s, save=save, key=key)

    def plot_filters(self, t, N_s, save=None, key=jrnd.PRNGKey(1)):
        ts = [jnp.vstack((t for i in range(gp.D))).T for gp in self.g_gps[0]]
        g_samps = self.sample_diag_g_gps([ts], N_s, key=key)[0]
        _, axs = plt.subplots(self.C[0], 1, figsize=(8, 5 * self.C[0]))
        for i in range(self.C[0]):
            if self.C[0] == 1:
                ax = axs
            else:
                ax = axs[i]
            y = g_samps[i].T * jnp.exp(-self.alpha[0] * (t) ** 2)
            ax.plot(t, y.T, c="red", alpha=0.5)
            ax.set_title(f"$G_{i}$")

        if save:
            plt.savefig(save)
        plt.show()


class IONVKM(VariationalNVKM):
    def __init__(self, zgs, u_data, y_data, q_class):
        raise NotImplementedError
