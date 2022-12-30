

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
from colanet import flow

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


class ColaNetLit(pl.LightningModule):

    # model = ModelLit(model_cfg,flow=cfg.flow,isize=cfg.isize,
    #                  batch_size=cfg.batch_size_tr,lr_init=cfg.lr_init,
    #                  weight_decay=cfg.weight_decay,nepochs=cfg.nepochs,
    #                  warmup_epochs=cfg.warmup_epochs,task=cfg.task,
    #                  uuid=str(cfg.uuid))

    def __init__(self,model_cfg,batch_size=1,flow=True,
                 ensemble=False,isize=None,bw=False,
                 lr_init=1e-3,weight_decay=1e-4,nepochs=0,
                 warmup_epochs=0,task=0,uuid="",
                 sim_type="g",sim_device="cuda:0"):
        super().__init__()
        self.lr_init = lr_init
        self._model = [colanet.load_model(model_cfg)]
        self.bw = bw
        self.net = self._model[0]#.model
        self.batch_size = batch_size
        self.flow = flow
        self.isize = isize
        self.gen_loger = logging.getLogger('lightning')
        self.gen_loger.setLevel("NOTSET")
        self.ca_fwd = "dnls_k"
        self.sim_model = self.get_sim_model(sim_type,sim_device)

    def get_sim_model(self,sim_type,sim_device):
        if sim_type == "g":
            return None
        elif sim_type == "stardeno":
            return stardeno.load_noise_sim(sim_device,True).to(sim_device)
        else:
            raise ValueError(f"Unknown sim model [{sim_type}]")

    def forward(self,vid):
        if self.ca_fwd == "dnls_k" or self.ca_fwd == "dnls":
            return self.forward_dnls_k(vid)
        elif self.ca_fwd == "default":
            return self.forward_default(vid)
        else:
            msg = f"Uknown ca forward type [{self.ca_fwd}]"
            raise ValueError(msg)

    def forward_dnls_k(self,vid):
        flows = self._get_flow(vid)
        deno = self.net(vid,flows=flows)
        deno = th.clamp(deno,0.,1.)
        return deno

    def forward_default(self,vid):
        flows = self._get_flow(vid)
        model = self._model[0]
        model.model = self.net
        if self.isize is None:
            deno = model.forward_chop(vid,flows=flows)
        else:
            deno = self.net(vid,flows=flows)
        deno = th.clamp(deno,0.,1.)
        return deno

    def _get_flow(self,vid):
        if self.flow == True:
            est_sigma = flow.est_sigma(vid)
            flows = flow.run_batch(vid[None,:],est_sigma)
        else:
            t,c,h,w = vid.shape
            zflows = th.zeros((1,t,2,h,w)).to(self.device)
            flows = edict()
            flows.fflow,flows.bflow = zflows,zflows
        return flows

    def sample_noisy(self,batch):
        if self.sim_model is None: return
        clean = batch['clean']
        noisy = self.sim_model.run_rgb(clean)
        batch['noisy'] = noisy

    def configure_optimizers(self):
        optim = th.optim.Adam(self.parameters(),lr=self.lr_init)
        StepLR = th.optim.lr_scheduler.StepLR
        scheduler = StepLR(optim, step_size=1000, gamma=0.1)
        return [optim], [scheduler]

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
        # print("noisy.shape: ",noisy.shape)

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
                 on_epoch=True,batch_size=1)
        self.log("val_mem_res", mem_res, on_step=False,
                 on_epoch=True,batch_size=1)
        self.log("val_mem_alloc", mem_alloc, on_step=False,
                 on_epoch=True,batch_size=1)


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
