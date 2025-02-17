
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

# -- caching results --
import cache_io

# -- network --
import colanet
import colanet.configs as configs
import colanet.utils.gpu_mem as gpu_mem
from colanet.utils.timer import ExpTimer
from colanet.utils.metrics import compute_psnrs,compute_ssims
from colanet.lightning import ColaNetLit,MetricsCallback
from colanet.utils.misc import rslice,write_pickle,read_pickle,optional

# -- lightning module --
import torch
import pytorch_lightning as pl
from pytorch_lightning import Callback
from pytorch_lightning.loggers import CSVLogger
from pytorch_lightning.callbacks import ModelCheckpoint,StochasticWeightAveraging
from pytorch_lightning.utilities.distributed import rank_zero_only

def launch_training(cfg):

    # -=-=-=-=-=-=-=-=-
    #
    #     Init Exp
    #
    # -=-=-=-=-=-=-=-=-

    # -- set seed --
    configs.set_seed(cfg.seed)

    # -- create timer --
    timer = ExpTimer()

    # -- init log dir --
    log_dir = Path(cfg.log_root) / str(cfg.uuid)
    if not log_dir.exists():
        log_dir.mkdir(parents=True)

    # -- prepare save directory for pickles --
    save_dir = Path("./output/training/") / cfg.uuid
    if not save_dir.exists():
        save_dir.mkdir(parents=True)

    # -- network --
    model_cfg = colanet.extract_model_config(cfg)
    model = ColaNetLit(model_cfg,cfg.batch_size,
                       cfg.flow=="true",cfg.ensemble=="true",
                       cfg.isize,cfg.bw)

    # -- load dataset with testing mods isizes --
    model.isize = None
    cfg_clone = copy.deepcopy(cfg)
    cfg_clone.isize = None
    cfg_clone.cropmode = "center"
    cfg_clone.nsamples_val = cfg.nsamples_at_testing
    data,loaders = data_hub.sets.load(cfg_clone)

    # -- init validation performance --
    init_val_report = MetricsCallback()
    logger = CSVLogger(log_dir,name="init_val_te",flush_logs_every_n_steps=1)
    trainer = pl.Trainer(gpus=1,precision=32,limit_train_batches=1.,
                         max_epochs=3,log_every_n_steps=1,
                         callbacks=[init_val_report],logger=logger)
    timer.start("init_val_te")
    trainer.test(model, loaders.val)
    timer.stop("init_val_te")
    init_val_results = init_val_report.metrics
    print("--- Init Validation Results ---")
    print(init_val_results)
    init_val_res_fn = save_dir / "init_val.pkl"
    write_pickle(init_val_res_fn,init_val_results)
    print(timer)

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    #
    #          Training
    #
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

    # -- reset model --
    model.isize = cfg.isize

    # -- data --
    data,loaders = data_hub.sets.load(cfg)
    print("Num Training Vids: ",len(data.tr))

    # -- pytorch_lightning training --
    logger = CSVLogger(log_dir,name="train",flush_logs_every_n_steps=1)
    chkpt_fn = cfg.uuid + "-{epoch:02d}-{val_loss:2.2e}"
    checkpoint_callback = ModelCheckpoint(monitor="val_loss",save_top_k=10,mode="min",
                                          dirpath=cfg.checkpoint_dir,filename=chkpt_fn)
    chkpt_fn = cfg.uuid + "-{epoch:02d}"
    cc_recent = ModelCheckpoint(monitor="epoch",save_top_k=10,mode="max",
                                dirpath=cfg.checkpoint_dir,filename=chkpt_fn)
    swa_callback = StochasticWeightAveraging(swa_lrs=cfg.lr_init)
    trainer = pl.Trainer(accelerator="gpu",devices=cfg.ndevices,precision=32,
                         accumulate_grad_batches=cfg.accumulate_grad_batches,
                         limit_train_batches=cfg.limit_train_batches,
                         max_epochs=cfg.nepochs-1,log_every_n_steps=1,
                         logger=logger,gradient_clip_val=0.5,
                         callbacks=[checkpoint_callback,swa_callback,cc_recent])
    timer.start("train")
    trainer.fit(model, loaders.tr, loaders.val)
    timer.stop("train")
    best_model_path = checkpoint_callback.best_model_path


    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    #
    #       Validation Testing
    #
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

    # -- reload dataset with no isizes --
    model.isize = None
    cfg_clone = copy.deepcopy(cfg)
    cfg_clone.isize = None
    # cfg_clone.cropmode = "center"
    cfg_clone.nsamples_tr = cfg.nsamples_at_testing
    cfg_clone.nsamples_val = cfg.nsamples_at_testing
    data,loaders = data_hub.sets.load(cfg_clone)

    # -- training performance --
    tr_report = MetricsCallback()
    logger = CSVLogger(log_dir,name="train_te",flush_logs_every_n_steps=1)
    trainer = pl.Trainer(gpus=1,precision=32,limit_train_batches=1.,
                         max_epochs=1,log_every_n_steps=1,
                         callbacks=[tr_report],logger=logger)
    timer.start("train_te")
    trainer.test(model, loaders.tr)
    timer.stop("train_te")
    tr_results = tr_report.metrics
    tr_res_fn = save_dir / "train.pkl"
    write_pickle(tr_res_fn,tr_results)

    # -- validation performance --
    val_report = MetricsCallback()
    logger = CSVLogger(log_dir,name="val_te",flush_logs_every_n_steps=1)
    trainer = pl.Trainer(gpus=1,precision=32,limit_train_batches=1.,
                         max_epochs=1,log_every_n_steps=1,
                         callbacks=[val_report],logger=logger)
    timer.start("val_te")
    trainer.test(model, loaders.val)
    timer.stop("val_te")
    val_results = val_report.metrics
    print("--- Tuned Validation Results ---")
    print(val_results)
    val_res_fn = save_dir / "val.pkl"
    write_pickle(val_res_fn,val_results)

    # -- report --
    results = edict()
    results.best_model_path = best_model_path
    results.init_val_results_fn = init_val_res_fn
    results.train_results_fn = tr_res_fn
    results.val_results_fn = val_res_fn
    results.train_time = timer["train"]
    results.test_train_time = timer["train_te"]
    results.test_val_time = timer["val_te"]
    results.test_init_val_time = timer["init_val_te"]

    return results

def main():

    # -- print os pid --
    print("PID: ",os.getpid())

    # -- init --
    verbose = True
    cache_dir = ".cache_io"
    # cache_name = "train_rgb_net" # without "rand_bwd" option
    # cache_name = "train_rgb_net_with_rand_bwd"  # _with_ "rand_bwd"
    cache_name = "train_rgb_net_with_rand_bwd"  # _with_ "rand_bwd"
    cache = cache_io.ExpCache(cache_dir,cache_name)
    # cache.clear()

    # -- create exp list --
    ws,wt,k = [27],[0],[100]
    # ws,wt,k = [20],[3],[100]
    sigmas = [30.]#,30.,10.]
    flow = ['false']
    isizes = ["128_128"]
    ca_fwd_list = ["stnls_k"]
    rand_bwd = ["false",]#"true","false"]
    exp_lists = {"sigma":sigmas,"ws":ws,"wt":wt,"k":k,
                 "isize":isizes,"ca_fwd":ca_fwd_list,'flow':flow,
                 "rand_bwd":rand_bwd}# new and optional!
    exps_a = cache_io.mesh_pydicts(exp_lists) # create mesh

    # -- default --
    exp_lists['ca_fwd'] = ['default']
    exp_lists['flow'] = ['false']
    exp_lists['isize'] = ['128_128']
    exp_lists['rand_bwd'] = ["false"]
    exps_b = cache_io.mesh_pydicts(exp_lists) # create mesh

    # -- try training "stnls_k" without flow --
    # exp_lists['ca_fwd'] = ['stnls_k']
    # exp_lists['flow'] = ['false']
    # exps_c = cache_io.mesh_pydicts(exp_lists) # create mesh

    # -- agg --
    # exps = exps_a + exps_b
    # exps = exps_a + exps_b# + exps_c
    exps = exps_a# + exps_b
    nexps = len(exps)

    # -- group with default --
    cfg = configs.default_train_cfg()
    cfg.dname = "davis_cropped"
    cfg.bw = False
    cfg.n_colors = 3
    cfg.nsamples_tr = 0
    cfg.nsamples_val = 30
    cfg.nepochs = 100
    cfg.ndevices = 1
    cfg.batch_size = 4
    cfg.batch_size_tr = 4//cfg.ndevices
    cfg.batch_size_val = 1
    cfg.batch_size_te = 1
    # cfg.lr_init = 1e-4
    cfg.lr_init = 1e-5
    cfg.model_type = "augmented"
    cfg.accumulate_grad_batches = 1
    cfg.limit_train_batches = .25 # current 1hr(ws=15), 2hr(ws=27)
    cfg.persistent_workers = True
    cfg.pretrained_load = False
    cfg.pretrained_path = "20c5e28c-3333-484c-b982-bca1bf25eb19-epoch=03.ckpt"
    cfg.pretrained_type = "lit"
    cache_io.append_configs(exps,cfg) # merge the two

    # -- launch each experiment --
    for exp_num,exp in enumerate(exps):

        # -- info --
        if verbose:
            print("-="*25+"-")
            print(f"Running experiment number {exp_num+1}/{nexps}")
            print("-="*25+"-")
            pp.pprint(exp)

        # -- check if loaded --
        uuid = cache.get_uuid(exp) # assing ID to each Dict in Meshgrid
        # cache.clear_exp(uuid)
        results = cache.load_exp(exp) # possibly load result

        # -- possibly continue from current epochs --
        # todo:

        # -- run experiment --
        if results is None: # check if no result
            exp.uuid = uuid
            results = launch_training(exp)
            cache.save_exp(uuid,exp,results) # save to cache

    # -- results --
    records = cache.load_flat_records(exps)
    print(records.columns)
    print(records['uuid'])
    print(records['best_model_path'].iloc[0])
    print(records['best_model_path'].iloc[1])


    # -- load res --
    uuids = list(records['uuid'].to_numpy())
    cas = list(records['ca_fwd'].to_numpy())
    fns = list(records['init_val_results_fn'].to_numpy())
    res_a = read_pickle(fns[0])
    res_b = read_pickle(fns[1])
    print(uuids)
    print(cas)
    print(res_a['test_psnr'])
    print(res_a['test_index'])
    print(res_b['test_psnr'])
    print(res_b['test_index'])

    fns = list(records['val_results_fn'].to_numpy())
    res_a = read_pickle(fns[0])
    res_b = read_pickle(fns[1])
    print(uuids,cas,fns)
    print(res_a['test_psnr'])
    print(res_a['test_index'])
    print(res_b['test_psnr'])
    print(res_b['test_index'])



# def find_records(path,uuid):
#     files = []
#     for fn in path.iterdir():
#         if uuid in fn:
#             files.append(fn)
#     for fn in files:
#         fn.



if __name__ == "__main__":
    main()
