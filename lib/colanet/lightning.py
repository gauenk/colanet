

# -- misc --
import os,math,tqdm
import pprint,copy
pp = pprint.PrettyPrinter(indent=4)

# -- linalg --
import numpy as np
import torch as th
from einops import rearrange,repeat

# -- data mngmnt --
from pathlib import Path
from easydict import EasyDict as edict

# -- data --
import data_hub

# -- optical flow --
# from colanet import flow
from dev_basics import flow

# -- caching results --
import cache_io

# -- network --
import colanet
import colanet.configs as configs
import colanet.utils.gpu_mem as gpu_mem
from colanet.utils.timer import ExpTimer
from colanet.utils.metrics import compute_psnrs,compute_ssims
from colanet.utils.misc import rslice,write_pickle,read_pickle

# -- noise sims --
try:
    import stardeno
except:
    pass

# -- generic logging --
import logging
logging.basicConfig()

# -- lightning module --
import torch
import pytorch_lightning as pl
from pytorch_lightning import Callback
from pytorch_lightning.loggers import CSVLogger
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.utilities.distributed import rank_zero_only




@econfig.set_init
def init_cfg(cfg):
    econfig.set_cfg(cfg)
    cfgs = econfig({"lit":lit_pairs(),
                    "sim":sim_pairs()})
    return cfgs

def lit_pairs():
    pairs = {"batch_size":1,"flow":True,"flow_method":"cv2",
             "isize":None,"bw":False,"lr_init":1e-3,
             "lr_final":1e-8,"weight_decay":0.,
             "nepochs":0,"task":"denoising","uuid":"",
             "scheduler":"default","step_lr_size":5,
             "step_lr_gamma":0.1,"flow_epoch":None,"flow_from_end":None}
    return pairs

def sim_pairs():
    pairs = {"sim_type":"g","sim_module":"stardeno",
             "sim_device":"cuda:0","load_fxn":"load_sim"}
    return pairs

def get_sim_model(self,cfg):
    if cfg.sim_type == "g":
        return None
    elif cfg.sim_type == "stardeno":
        module = importlib.load_module(cfg.sim_module)
        return module.load_noise_sim(cfg.sim_device,True).to(cfg.sim_device)
    else:
        raise ValueError(f"Unknown sim model [{sim_type}]")

class ColaNetLit(pl.LightningModule):

    def __init__(self,model_cfg,batch_size=1,
                 flow=True,flow_method="cv2",isize=None,bw=False,
                 lr_init=1e-3,lr_final=1e-8,weight_decay=1e-4,nepochs=0,
                 warmup_epochs=0,scheduler="default",momentum=0.,
                 task=0,uuid="",sim_type="g",sim_device="cuda:0",
                 optim="default",deno_clamp=False):
        super().__init__()
        self.optim = optim
        self.lr_init = lr_init
        self.lr_final = lr_final
        self.weight_decay = weight_decay
        self.momentum = momentum
        self.scheduler = scheduler
        self.nepochs = nepochs
        self._model = [colanet.load_model(model_cfg)]
        self.bw = bw
        self.net = self._model[0]#.model
        self.batch_size = batch_size
        self.flow = flow
        self.flow_method = flow_method
        self.isize = isize
        self.gen_loger = logging.getLogger('lightning')
        self.gen_loger.setLevel("NOTSET")
        self.ca_fwd = "stnls_k"
        self.sim_model = self.get_sim_model(sim_type,sim_device)
        self.deno_clamp = deno_clamp

    def get_sim_model(self,sim_type,sim_device):
        if sim_type == "g":
            return None
        elif sim_type == "stardeno":
            return stardeno.load_noise_sim(sim_device,True).to(sim_device)
        else:
            raise ValueError(f"Unknown sim model [{sim_type}]")

    def forward(self,vid):
        if self.ca_fwd == "stnls_k" or self.ca_fwd == "stnls":
            return self.forward_stnls_k(vid)
        elif self.ca_fwd == "default":
            return self.forward_default(vid)
        else:
            msg = f"Uknown ca forward type [{self.ca_fwd}]"
            raise ValueError(msg)

    def forward_stnls_k(self,vid):
        flows = flow.orun(vid,self.flow,ftype=self.flow_method)
        deno = self.net(vid,flows=flows)
        deno = th.clamp(deno,0.,1.)
        return deno

    def forward_default(self,vid):
        flows = flow.orun(vid,self.flow,ftype=self.flow_method)
        model = self._model[0]
        model.model = self.net
        if self.isize is None:
            deno = model.forward_chop(vid,flows=flows)
        else:
            deno = self.net(vid,flows=flows)
        if self.deno_clamp:
            deno = th.clamp(deno,0.,1.)
        return deno

    def sample_noisy(self,batch):
        if self.sim_model is None: return
        clean = batch['clean']
        noisy = self.sim_model.run_rgb(clean)
        batch['noisy'] = noisy

    def configure_optimizers(self):
        if self.optim in ["default","adam"]:
            optim = th.optim.Adam(self.parameters(),lr=self.lr_init,
                                   weight_decay=self.weight_decay)
        elif self.optim in ["adamw"]:
            optim = th.optim.AdamW(self.parameters(),lr=self.lr_init,
                                   weight_decay=self.weight_decay)
        else:
            raise ValueError("Uknown optimizer [%s]" % self.optim)
        sched = self.configure_scheduler(optim)
        return [optim], [sched]

    def configure_scheduler(self,optim):
        if self.scheduler in ["default","exp_decay"]:
            gamma = 1-math.exp(math.log(self.lr_final/self.lr_init)/self.nepochs)
            ExponentialLR = th.optim.lr_scheduler.ExponentialLR
            scheduler = ExponentialLR(optim,gamma=gamma) # (.995)^50 ~= .78
        elif self.scheduler in ["step","steplr"]:
            StepLR = th.optim.lr_scheduler.StepLR
            scheduler = StepLR(optim,step_size=5,gamma=0.1)
        elif self.scheduler in ["cos"]:
            CosAnnLR = th.optim.lr_scheduler.CosineAnnealingLR
            T0,Tmult = 1,1
            scheduler = CosAnnLR(optim,T0,Tmult)
        elif self.scheduler in ["none"]:
            StepLR = th.optim.lr_scheduler.StepLR
            scheduler = StepLR(optim,step_size=10**3,gamma=1.)
        else:
            raise ValueError(f"Uknown scheduler [{self.scheduler}]")
        return scheduler

    def training_step(self, batch, batch_idx):

        # -- sample noise from simulator --
        self.sample_noisy(batch)

        # -- each sample in batch --
        loss = 0 # init @ zero
        nbatch = len(batch['noisy'])
        denos,cleans = [],[]
        for i in range(nbatch):
            deno_i,clean_i,loss_i = self.training_step_i(batch, i)
            loss += loss_i
            denos.append(deno_i)
            cleans.append(clean_i)
        loss = loss / nbatch

        # -- append --
        denos = th.stack(denos)
        cleans = th.stack(cleans)

        # -- log --
        self.log("train_loss", loss.item(), on_step=True,
                 on_epoch=False,batch_size=self.batch_size)

        # -- terminal log --
        val_psnr = np.mean(compute_psnrs(denos,cleans,div=1.)).item()
        self.gen_loger.info("train_psnr: %2.2f" % val_psnr)
        # print("train_psnr: %2.2f" % val_psnr)
        self.log("train_loss", loss.item(), on_step=True,
                 on_epoch=False, batch_size=self.batch_size)

        return loss

    def training_step_i(self, batch, i):

        # -- unpack batch
        noisy = batch['noisy'][i]/255.
        clean = batch['clean'][i]/255.
        region = batch['region'][i]

        # -- get data --
        noisy = rslice(noisy,region)
        clean = rslice(clean,region)

        # -- foward --
        deno = self.forward(noisy)

        # -- report loss --
        loss = th.mean((clean - deno)**2)
        return deno.detach(),clean,loss

    def validation_step(self, batch, batch_idx):

        # -- sample noise from simulator --
        self.sample_noisy(batch)

        # -- denoise --
        noisy,clean = batch['noisy'][0]/255.,batch['clean'][0]/255.
        region = batch['region'][0]
        noisy = rslice(noisy,region)
        clean = rslice(clean,region)

        # -- forward --
        gpu_mem.print_peak_gpu_stats(False,"val",reset=True)
        with th.no_grad():
            deno = self.forward(noisy)
        mem_res,mem_alloc = gpu_mem.print_peak_gpu_stats(False,"val",reset=True)

        # -- loss --
        loss = th.mean((clean - deno)**2)

        # -- report --
        self.log("val_loss", loss.item(), on_step=False,
                 on_epoch=True,batch_size=1,sync_dist=True)
        self.log("val_mem_res", mem_res, on_step=False,
                 on_epoch=True,batch_size=1,sync_dist=True)
        self.log("val_mem_alloc", mem_alloc, on_step=False,
                 on_epoch=True,batch_size=1,sync_dist=True)

        # -- terminal log --
        val_psnr = np.mean(compute_psnrs(deno,clean,div=1.)).item()
        self.gen_loger.info("val_psnr: %2.2f" % val_psnr)

    def test_step(self, batch, batch_nb):

        # -- sample noise from simulator --
        self.sample_noisy(batch)

        # -- denoise --
        index,region = batch['index'][0],batch['region'][0]
        noisy,clean = batch['noisy'][0]/255.,batch['clean'][0]/255.
        noisy = rslice(noisy,region)
        clean = rslice(clean,region)

        # -- forward --
        gpu_mem.print_peak_gpu_stats(False,"test",reset=True)
        with th.no_grad():
            deno = self.forward(noisy)
        mem_res,mem_alloc = gpu_mem.print_peak_gpu_stats(False,"test",reset=True)

        # -- compare --
        loss = th.mean((clean - deno)**2)
        psnr = np.mean(compute_psnrs(deno,clean,div=1.)).item()
        ssim = np.mean(compute_ssims(deno,clean,div=1.)).item()

        # -- terminal log --
        self.log("psnr", psnr, on_step=True, on_epoch=False, batch_size=1)
        self.log("ssim", ssim, on_step=True, on_epoch=False, batch_size=1)
        self.log("index",  int(index.item()),on_step=True,on_epoch=False,batch_size=1)
        self.log("mem_res",  mem_res, on_step=True, on_epoch=False, batch_size=1)
        self.log("mem_alloc",  mem_alloc, on_step=True, on_epoch=False, batch_size=1)
        self.gen_loger.info("te_psnr: %2.2f" % psnr)

        # -- log --
        results = edict()
        results.test_loss = loss.item()
        results.test_psnr = psnr
        results.test_ssim = ssim
        results.test_mem_alloc = mem_alloc
        results.test_mem_res = mem_res
        results.test_index = index.cpu().numpy().item()
        return results

class MetricsCallback(Callback):
    """PyTorch Lightning metric callback."""

    def __init__(self):
        super().__init__()
        self.metrics = {}

    def _accumulate_results(self,each_me):
        for key,val in each_me.items():
            if not(key in self.metrics):
                self.metrics[key] = []
            if hasattr(val,"ndim"):
                ndim = val.ndim
                val = val.cpu().numpy().item()
            self.metrics[key].append(val)

    @rank_zero_only
    def log_metrics(self, metrics, step):
        # metrics is a dictionary of metric names and values
        # your code to record metrics goes here
        print("logging metrics: ",metrics,step)

    def on_train_epoch_end(self, trainer, pl_module):
        each_me = copy.deepcopy(trainer.callback_metrics)
        self._accumulate_results(each_me)

    def on_validation_epoch_end(self, trainer, pl_module):
        each_me = copy.deepcopy(trainer.callback_metrics)
        self._accumulate_results(each_me)

    def on_test_epoch_end(self, trainer, pl_module):
        each_me = copy.deepcopy(trainer.callback_metrics)
        self._accumulate_results(each_me)

    def on_train_batch_end(self, trainer, pl_module, outs,
                           batch, batch_idx, dl_idx):
        each_me = copy.deepcopy(trainer.callback_metrics)
        self._accumulate_results(each_me)


    def on_validation_batch_end(self, trainer, pl_module, outs,
                                batch, batch_idx, dl_idx):
        each_me = copy.deepcopy(trainer.callback_metrics)
        self._accumulate_results(each_me)

    def on_test_batch_end(self, trainer, pl_module, outs,
                          batch, batch_idx, dl_idx):
        self._accumulate_results(outs)



def remove_lightning_load_state(state):
    names = list(state.keys())
    for name in names:
        name_new = name.split(".")[1:]
        name_new = ".".join(name_new)
        state[name_new] = state[name]
        del state[name]
