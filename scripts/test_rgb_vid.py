
# -- misc --
import os,math,tqdm
import pprint
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
import svnlb

# -- caching results --
import cache_io

# -- network --
import colanet
import colanet.configs as configs
from colanet import lightning
from colanet.utils.misc import optional
from colanet.utils.misc import rslice,write_pickle,read_pickle

def run_exp(cfg):

    # -- set device --
    th.cuda.set_device(int(cfg.device.split(":")[1]))

    # -- init results --
    results = edict()
    results.psnrs = []
    results.ssims = []
    results.noisy_psnrs = []
    results.adapt_psnrs = []
    results.deno_fns = []
    results.vid_frames = []
    results.vid_name = []
    results.timer_flow = []
    results.timer_adapt = []
    results.timer_deno = []

    # -- network --
    nchnls = 1 if cfg.bw else 3
    model = colanet.refactored.load_model(cfg.mtype,cfg.sigma,2,nchnls).to(cfg.device)
    model.eval()
    imax = 255.
    model.model.body[8].ca_forward_type = cfg.ca_fwd
    model.chop = cfg.ca_fwd == "default"

    # -- optional load trained weights --
    load_trained_state(model,cfg.use_train,cfg.ca_fwd,cfg.sigma,cfg.ws,cfg.wt)

    # -- data --
    data,loaders = data_hub.sets.load(cfg)
    groups = data.te.groups
    indices = [i for i,g in enumerate(groups) if cfg.vid_name in g]

    # -- optional filter --
    frame_start = optional(cfg,"frame_start",0)
    frame_end = optional(cfg,"frame_end",0)
    if frame_start >= 0 and frame_end > 0:
        def fbnds(fnums,lb,ub): return (lb <= np.min(fnums)) and (ub >= np.max(fnums))
        indices = [i for i in indices if fbnds(data.te.paths['fnums'][groups[i]],
                                               cfg.frame_start,cfg.frame_end)]
    for index in indices:

        # -- clean memory --
        th.cuda.empty_cache()
        print("index: ",index)

        # -- unpack --
        sample = data.te[index]
        region = sample['region']
        noisy,clean = sample['noisy'],sample['clean']
        noisy,clean = noisy.to(cfg.device),clean.to(cfg.device)
        vid_frames = sample['fnums']
        print("[%d] noisy.shape: " % index,noisy.shape)

        # -- optional crop --
        noisy = rslice(noisy,region)
        clean = rslice(clean,region)
        print("[%d] noisy.shape: " % index,noisy.shape)

        # -- create timer --
        timer = colanet.utils.timer.ExpTimer()

        # -- size --
        nframes = noisy.shape[0]
        ngroups = int(25 * 37./nframes)
        batch_size = 390*39#ngroups*1024

        # -- optical flow --
        timer.start("flow")
        if cfg.flow == "true":
            noisy_np = noisy.cpu().numpy()
            if noisy_np.shape[1] == 1:
                noisy_np = np.repeat(noisy_np,3,axis=1)
            flows = svnlb.compute_flow(noisy_np,cfg.sigma)
            flows = edict({k:th.from_numpy(v).to(cfg.device) for k,v in flows.items()})
        else:
            flows = None
        timer.stop("flow")

        # -- internal adaptation --
        timer.start("adapt")
        run_internal_adapt = cfg.internal_adapt_nsteps > 0
        run_internal_adapt = run_internal_adapt and (cfg.internal_adapt_nepochs > 0)
        adapt_psnrs = [0.]
        if run_internal_adapt:
            adapt_psnrs = model.run_internal_adapt(
                noisy,cfg.sigma,flows=flows,
                ws=cfg.ws,wt=cfg.wt,batch_size=batch_size,
                nsteps=cfg.internal_adapt_nsteps,
                nepochs=cfg.internal_adapt_nepochs,
                sample_mtype=cfg.adapt_mtype,
                clean_gt = clean,
                region_gt = [2,4,128,256,256,384]
            )
        timer.stop("adapt")

        # -- denoise --
        batch_size = 390*100
        timer.start("deno")
        with th.no_grad():
            deno = model(noisy/imax,flows=flows)*imax
        timer.stop("deno")
        deno = deno.clamp(0.,imax)

        # -- save example --
        out_dir = Path(cfg.saved_dir) / str(cfg.uuid)
        deno_fns = colanet.utils.io.save_burst(deno,out_dir,"deno")

        # -- psnr --
        noisy_psnrs = colanet.utils.metrics.compute_psnrs(noisy,clean,div=imax)
        psnrs = colanet.utils.metrics.compute_psnrs(deno,clean,div=imax)
        ssims = colanet.utils.metrics.compute_ssims(deno,clean,div=imax)
        print(noisy_psnrs)
        print(psnrs)

        # -- append results --
        results.psnrs.append(psnrs)
        results.ssims.append(ssims)
        results.noisy_psnrs.append(noisy_psnrs)
        results.adapt_psnrs.append(adapt_psnrs)
        results.deno_fns.append(deno_fns)
        results.vid_frames.append(vid_frames)
        results.vid_name.append([cfg.vid_name])
        for name,time in timer.items():
            results[name].append(time)

    return results

def load_trained_state(model,use_train,ca_fwd,sigma,ws,wt):

    # -- skip if needed --
    if not(use_train == "true"): return

    # -- open training cache info --
    cache_dir = ".cache_io"
    cache_name = "train_rgb_net" # current!
    cache = cache_io.ExpCache(cache_dir,cache_name)

    # -- create config --
    cfg = configs.default_train_cfg()
    cfg.bw = True
    cfg.ws = ws
    cfg.wt = wt
    cfg.sigma = sigma
    cfg.isize = "128_128" # a fixed training parameters
    cfg.ca_fwd = ca_fwd

    # -- read cache --
    results = cache.load_exp(cfg) # possibly load result
    if ca_fwd == "dnls_k":
        model_path = "output/checkpoints/5acb13a6-4771-4633-9192-8a8c53df975c-epoch=11-val_loss=2.83e-03.ckpt"
    elif ca_fwd == "default":
        model_path = "output/checkpoints/dba41c7b-8e9e-4b0d-9e14-9005fc0dd908-epoch=00-val_loss=2.08e-03.ckpt"
    else:
        raise ValueError(f"Uknown ca_fwd [{ca_fwd}]")

    # -- load model state --
    state = th.load(model_path)['state_dict']
    lightning.remove_lightning_load_state(state)
    model.model.load_state_dict(state)
    return model

def main():

    # -- (0) start info --
    verbose = True
    pid = os.getpid()
    print("PID: ",pid)

    # -- get cache --
    cache_dir = ".cache_io"
    cache_name = "test_rgb_net" # current!
    cache = cache_io.ExpCache(cache_dir,cache_name)
    # cache.clear()

    # -- get defaults --
    cfg = configs.default_test_vid_cfg()
    cfg.isize = "none"#"128_128"
    cfg.bw = True
    cfg.nframes = 4
    cfg.frame_start = 0
    cfg.frame_end = cfg.nframes-1

    # -- get mesh --
    dnames = ["set8"]
    vid_names = ["tractor"]
    # vid_names = ["snowboard","sunflower","tractor","motorbike",
    #              "hypersmooth","park_joy","rafting","touchdown"]
    internal_adapt_nsteps = [300]
    internal_adapt_nepochs = [0]
    ws,wt,sigmas = [10],[5],[50.]
    flow,isizes,adapt_mtypes = ["true"],["none"],["rand"]
    ca_fwd_list,use_train = ["dnls_k"],["true"]
    exp_lists = {"dname":dnames,"vid_name":vid_names,"sigma":sigmas,
                 "internal_adapt_nsteps":internal_adapt_nsteps,
                 "internal_adapt_nepochs":internal_adapt_nepochs,
                 "flow":flow,"ws":ws,"wt":wt,"adapt_mtype":adapt_mtypes,
                 "isize":isizes,"use_train":use_train,
                 "ca_fwd":ca_fwd_list}
    exps_a = cache_io.mesh_pydicts(exp_lists) # create mesh
    cache_io.append_configs(exps_a,cfg) # merge the two

    # -- original w/out training --
    exp_lists['use_train'] = ["false"]
    exp_lists['ca_fwd'] = ["default"]
    exps_b = cache_io.mesh_pydicts(exp_lists) # create mesh
    cfg.bw = True
    cache_io.append_configs(exps_b,cfg) # merge the two

    # -- cat exps --
    exps = exps_a + exps_b

    # -- run exps --
    nexps = len(exps)
    for exp_num,exp in enumerate(exps):

        # -- info --
        if verbose:
            print("-="*25+"-")
            print(f"Running experiment number {exp_num+1}/{nexps}")
            print("-="*25+"-")
            pp.pprint(exp)

        # -- logic --
        uuid = cache.get_uuid(exp) # assing ID to each Dict in Meshgrid
        # cache.clear_exp(uuid)
        results = cache.load_exp(exp) # possibly load result
        if results is None: # check if no result
            exp.uuid = uuid
            results = run_exp(exp)
            cache.save_exp(uuid,exp,results) # save to cache

    # -- load results --
    records = cache.load_flat_records(exps)
    # print(records)
    # print(records.filter(like="timer"))

    # -- viz report --
    for use_train,tdf in records.groupby("use_train"):
        for ca_group,gdf in tdf.groupby("ca_fwd"):
            agg_psnrs,agg_ssims = [],[]
            for vname,vdf in gdf.groupby("vid_name"):
                psnrs = np.stack(vdf['psnrs'])
                ssims = np.stack(vdf['ssims'])
                psnr_mean = psnrs.mean().item()
                ssim_mean = ssims.mean().item()
                # print(vname,psnr_mean,ssim_mean)
                agg_psnrs.append(psnr_mean)
                agg_ssims.append(ssim_mean)
            print(ca_group)
            psnr_mean = np.mean(agg_psnrs)
            ssim_mean = np.mean(agg_ssims)
            uuid = gdf['uuid']
            print(psnr_mean,ssim_mean,uuid)


if __name__ == "__main__":
    main()
