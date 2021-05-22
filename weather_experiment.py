#%%
from scipy.io import loadmat
import matplotlib.pyplot as plt
from nvkm.utils import l2p, make_zg_grids, RMSE, NMSE, gaussian_NLPD
from nvkm.models import MOVarNVKM
from nvkm.experiments import WeatherDataSet

from jax.config import config

config.update("jax_enable_x64", True)
import jax.numpy as jnp
import jax.random as jrnd
import matplotlib.pyplot as plt
import pandas as pd
import argparse
import copy
import pickle

parser = argparse.ArgumentParser(description="Weather MO experiment.")
parser.add_argument("--Nvu", default=100, type=int)
parser.add_argument("--Nvgs", default=[15, 10, 6], nargs="+", type=int)
parser.add_argument("--zgrange", default=[0.463, 0.372, 0.239], nargs="+", type=float)
parser.add_argument("--zurange", default=2.0, type=float)
parser.add_argument("--Nits", default=10000, type=int)
parser.add_argument("--lr", default=1e-3, type=float)
parser.add_argument("--Nbatch", default=80, type=int)
parser.add_argument("--Nbasis", default=30, type=int)
parser.add_argument("--Ns", default=10, type=int)
parser.add_argument("--ampgs", default=[5.0, 5.0, 5.0], nargs="+", type=float)
parser.add_argument("--q_frac", default=0.8, type=float)
parser.add_argument("--noise", default=0.05, type=float)
parser.add_argument("--f_name", default="weather", type=str)
parser.add_argument("--data_dir", default="data", type=str)
parser.add_argument("--key", default=102, type=int)
args = parser.parse_args()

Nbatch = args.Nbatch
Nbasis = args.Nbasis
noise = args.noise
Nits = args.Nits
Nvu = args.Nvu
Nvgs = args.Nvgs
zgran = args.zgrange
zuran = args.zurange
Ns = args.Ns
lr = args.lr
q_frac = args.q_frac
f_name = args.f_name
data_dir = args.data_dir
ampgs = args.ampgs
key = args.key
print(args)

# Nbatch = 50
# Nbasis = 30
# noise = 0.05
# Nits = 500
# Nvu = 80
# Ns = 5
# lr = 1e-2
# q_frac = 0.5
# f_name = "plots/res_test/hey"
# data_dir = "data"
# Nvgs = [15]
# zgran = [0.5]
# zuran = 2.0
# ampgs = [5.0]
# key = 1


data = WeatherDataSet(data_dir)
keys = jrnd.split(jrnd.PRNGKey(key), 6)
# %%
O = data.O
C = len(Nvgs)

zu = jnp.linspace(-zuran, zuran, Nvu).reshape(-1, 1)
lsu = zu[1][0] - zu[0][0]

tgs, lsgs = make_zg_grids(zgran, Nvgs)

model = MOVarNVKM(
    [tgs] * O,
    zu,
    (data.strain_x, data.strain_y),
    q_pars_init=None,
    q_initializer_pars=q_frac,
    q_init_key=keys[0],
    lsgs=[lsgs] * O,
    noise=[noise] * O,
    ampgs=[ampgs] * O,
    alpha=[[3 / (zgran[i]) ** 2 for i in range(C)]] * O,
    lsu=lsu,
    ampu=1.0,
    N_basis=Nbasis,
)
#%%
model.fit(Nits, lr, Nbatch, Ns, dont_fit=["lsu", "noise"], key=keys[1])
model.fit(
    int(Nits / 10),
    lr,
    Nbatch,
    Ns,
    dont_fit=["q_pars", "ampgs", "lsgs", "ampu", "lsu"],
    key=keys[5],
)
model.save(f_name + "_model.pkl")
print(model.noise)
print(model.ampu)
print(model.lsu)
print(model.ampgs)
print(model.lsgs)

# %%

axs = model.plot_samples(
    jnp.linspace(-zuran, zuran, 300),
    [jnp.linspace(-zuran, zuran, 300)] * O,
    Ns,
    return_axs=True,
    key=keys[2],
)
axs[2].scatter(data.stest_x[1], data.stest_y[1], c="red", alpha=0.3)
axs[3].scatter(data.stest_x[2], data.stest_y[2], c="red", alpha=0.3)
plt.savefig(f_name + "_samples.pdf")
plt.show()

#%%
model.plot_filters(
    jnp.linspace(-max(zgran), max(zgran), 60),
    10,
    save=f_name + "_filters.pdf",
    key=keys[3],
)

train_spreds = model.predict(data.strain_x, 50, key=keys[4])
_, train_pred_mean = data.upscale(data.strain_x, train_spreds[0])
train_pred_var = data.upscale_variance(train_spreds[1])

train_nmse = sum([NMSE(train_pred_mean[i], data.train_y[i]) for i in range(O)]) / 2
train_nlpd = (
    sum(
        [
            gaussian_NLPD(train_pred_mean[i], train_pred_var[i], data.train_y[i])
            for i in range(O)
        ]
    )
    / 2
)

#%%
spreds = model.predict(data.stest_x, 50, key=keys[4])
_, pred_mean = data.upscale(data.stest_x, spreds[0])
pred_var = data.upscale_variance(spreds[1])
#%%

fig, axs = plt.subplots(2, 1, figsize=(5, 5))
for i in range(2):
    axs[i].plot(
        data.test_x[i + 1], data.test_y[i + 1], c="black", ls=":", label="Val. Data"
    )
    axs[i].plot(data.test_x[i + 1], pred_mean[i + 1], c="green", label="Pred. Mean")
    axs[i].fill_between(
        data.test_x[i + 1],
        pred_mean[i + 1] + 2 * jnp.sqrt(pred_var[i + 1]),
        pred_mean[i + 1] - 2 * jnp.sqrt(pred_var[i + 1]),
        alpha=0.1,
        color="green",
        label="$\pm 2 \sigma$",
    )

plt.savefig(f_name + "_main.pdf")
# %%
print(f"Cambermet NMSE: {NMSE(pred_mean[1], data.test_y[1]):.2f}")
print(f"Chimet NMSE: {NMSE(pred_mean[2], data.test_y[2]):.2f}")
print(f"Cambermet NLPD: {gaussian_NLPD(pred_mean[1], pred_var[1], data.test_y[1]):.2f}")
print(f"Chimet NLPD: {gaussian_NLPD(pred_mean[2], pred_var[2], data.test_y[2]):.2f}")

# %%
test_nmse = sum([NMSE(pred_mean[i], data.test_y[i]) for i in range(1, 3)]) / 2
test_nlpd = (
    sum([gaussian_NLPD(pred_mean[i], pred_var[i], data.test_y[i]) for i in range(1, 3)])
    / 2
)

res = {
    "test NMSE": test_nmse,
    "train NMSE": train_nmse,
    "test NLPD": test_nlpd,
    "train NLPD": train_nlpd,
}
print(res)

with open(f_name + "res.pkl", "wb") as f:
    pickle.dump(res, f)


# %%
