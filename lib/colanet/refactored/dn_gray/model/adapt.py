"""
Functions for internal domain adaptation.

"""

# -- misc --
import sys,math,gc
from ..misc import default_options,crop_offset
from ..misc import default_options as get_default_config

# -- data structs --
import torch.utils.data as data
from colanet.utils.adapt_data import ImagePairDataSet
from colanet.utils.adapt_rpd import RegionProposalData
from colanet.utils.misc import assert_nonan

# -- linalg --
import torch as th
import numpy as np
from einops import repeat,rearrange

# -- path mgmnt --
from pathlib import Path

# -- separate class and logic --
from colanet.utils import clean_code
__methods__ = [] # self is a DataStore
register_method = clean_code.register_method(__methods__)


# -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
#
#    Run Adaptation of the Network to Image
#
# -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

@register_method
def run_internal_adapt(self,_noisy,sigma,srch_img=None,flows=None,ws=29,wt=0,
                       batch_size = -1, nsteps=100, nepochs=5, noise_sim = None,
                       sample_mtype="default", region_template = "2_96_96",
                       sobel_nlevels = 3, clean_gt=None, region_gt=None,
                       ensemble=False, verbose=False):
    if verbose: print("Running Internal Adaptation.")
    # noisy = (_noisy/255. - 0.5)/0.5
    div = 1.
    noisy = _noisy/div
    if not(clean_gt is None): _clean_gt =  clean_gt/div
    else: _clean_gt = None
    opt = get_default_config(sigma)
    append_adapts_cfg(opt)
    nadapts = 1
    if not(srch_img is None):
        _srch_img = srch_img/div
        _srch_img = _srch_img.contiguous()
    else: _srch_img = noisy

    for astep in range(nadapts):
        with th.no_grad():
            batch_size_no_grad = 390*39
            clean_raw = self(noisy,ensemble=ensemble,flows=flows)
            # clean_raw = self(noisy,sigma,_srch_img,flows=flows,rescale=False,
            #              ws=ws,wt=wt,batch_size=batch_size_no_grad)
        clean = clean_raw.detach().clamp(0., 1.)
        psnrs = adapt_step(self, clean, _srch_img, flows, opt,
                           ws=ws, wt=wt, batch_size=batch_size,
                           nsteps=nsteps,nepochs=nepochs,
                           noise_sim = noise_sim,
                           sample_mtype = sample_mtype,
                           sobel_nlevels = sobel_nlevels,
                           region_template=region_template,
                           noisy_gt=noisy,clean_gt=_clean_gt,
                           region_gt=region_gt,
                           ensemble=ensemble,verbose=verbose)
        return psnrs

@register_method
def run_external_adapt(self,_clean,sigma,srch_img=None,flows=None,ws=29,wt=0,
                       batch_size = -1, nsteps=100, nepochs=5, noise_sim=None,
                       sample_mtype="default", region_template = "2_96_96",
                       noisy_gt=None,sobel_nlevels = 3, ensemble=False, verbose=False):

    if verbose: print("Running External Adaptation.")
    # -- setup --
    opt = get_default_config(sigma)
    append_adapts_cfg(opt)
    nadapts = 1
    div = 1.
    clean = _clean/div
    # -- adapt --
    if not(srch_img is None):
        _srch_img = srch_img.contiguous()
        _srch_img = _srch_img/div
    else: _srch_img = clean

    # -- eval before --
    noisy = add_noise_to_image(clean, noise_sim, opt.sigma)
    # eval_nl(self,noisy,clean,_srch_img,flows,opt.sigma,verbose)

    for astep in range(nadapts):
        adapt_step(self, clean, _srch_img, flows, opt,
                   ws=ws,wt=wt, batch_size=batch_size,
                   nsteps=nsteps,nepochs=nepochs,
                   noise_sim = noise_sim,
                   sample_mtype = sample_mtype,
                   sobel_nlevels = sobel_nlevels,
                   region_template=region_template,
                   noisy_gt=noisy_gt,clean_gt=clean,
                   ensemble=ensemble,verbose=verbose)

def rslice(vid,coords):
    if coords is None: return vid
    fs,fe,t,l,b,r = coords
    return vid[fs:fe,:,t:b,l:r]

def compute_psnr(vid_a,vid_b):
    t = vid_a.shape[0]
    mse = th.mean((vid_a.reshape(t,-1)/2. - vid_b.reshape(t,-1)/2.)**2)
    psnr = -10 * th.log10(mse)
    psnr = psnr.cpu().numpy()
    return psnr

def adapt_step(nl_denoiser, clean, srch_img, flows, opt,
               ws=29, wt=0, nsteps=100, nepochs=5, batch_size=-1,
               noise_sim = None, sobel_nlevels = 3,
               sample_mtype="default", region_template = "2_64_64",
               noisy_gt=None,clean_gt=None,region_gt=None,
               ensemble=False, verbose=False):

    # -- psnrs --
    psnrs = []
    if not(clean_gt is None):
        psnr0 = compute_psnr(clean,clean_gt)
        print(psnr0)
        psnrs.append(psnr0)

    # -- optims --
    criterion = th.nn.MSELoss(reduction='mean')
    optim = th.optim.Adam(nl_denoiser.parameters(), lr=opt.lr,
                              betas=(0.9, 0.999), eps=1e-8)

    # -- get data --
    loader = get_adapt_dataset(clean,sample_mtype,region_template,sobel_nlevels)

    # -- train --
    noisy = add_noise_to_image(clean, noise_sim, opt.sigma)

    # -- epoch --
    for epoch in range(nepochs):

        # -- info --
        if verbose:
            print('Training epoch {} of {}'.format(epoch + 1, nepochs))

        # -- garbage collect --
        sys.stdout.flush()
        gc.collect()
        th.cuda.empty_cache()

        # -- loaders --
        device = next(nl_denoiser.parameters()).device
        iloader = enumerate(loader)
        nsamples = min(len(loader),nsteps)
        for i, region in iloader:

            # -- tenors on device --
            noisy_i = add_noise_to_image(clean,noise_sim,opt.sigma)
            noisy_r = rslice(noisy_i,region)

            # -- forward pass --
            optim.zero_grad()
            image_dn = nl_denoiser(noisy_r,ensemble=ensemble,flows=flows)
            # image_dn = nl_denoiser(noisy_i,opt.sigma,srch_img=None,flows=flows,
            #                        ws=ws,wt=wt,train=True,rescale=False,
            #                        batch_size=batch_size,region=region)

            # -- post-process images --
            image_dn = image_dn.clamp(0,1)
            clean_r = rslice(clean,region)

            # -- compute loss --
            loss = th.log10(criterion(image_dn, clean_r))
            assert not np.isnan(loss.item())

            # -- update step --
            loss.backward()
            optim.step()

            # -- memory dump --
            gc.collect()
            th.cuda.empty_cache()

            # -- logging --
            if (i % 10 == 0) or (nsteps == i):
                with th.no_grad():
                    batch_size_te = 390*100
                    if not(noisy_gt is None):
                        deno_gt =nl_denoiser(noisy_gt,ensemble=ensemble,flows=flows)
                        # deno_gt =nl_denoiser(noisy_gt,opt.sigma,srch_img=None,flows=flows,
                        #                       ws=ws,wt=wt,train=False,rescale=False,
                        #                       batch_size=batch_size_te,region=region_gt)
                        clean_gt_r = rslice(clean_gt,region_gt)
                        psnr_gt = compute_psnr(deno_gt,clean_gt_r)
                    else: psnr_gt = np.zeros(clean.shape[0])
                    psnrs.append(psnr_gt)

            # -- message --
            if verbose:
                print("Processing [%d/%d]: %2.2f" % (i,nsamples,-10*loss.item()))
            batch_bool = i == nsteps
            epoch_bool = (epoch + 1) % opt.epochs_between_check == 0
            print_bool = batch_bool and epoch_bool
            if print_bool:
                gc.collect()
                th.cuda.empty_cache()
                batch_size_te = 390*100
                with th.no_grad():
                    deno = nl_denoiser(noisy,ensemble=ensemble,flows=flows)
                    # deno = nl_denoiser(noisy,opt.sigma,srch_img.clone(),flows,
                    #                    rescale=False,ws=ws,wt=wt,
                    #                    batch_size=batch_size_te)
                deno = deno.detach().clamp(0., 1.)
                mse = criterion(deno,clean).item()
                train_psnr = -10 * math.log10(mse)
                psnrs.append(train_psnr)
                if verbose:
                    a,b,c = epoch + 1, nepochs, train_psnr
                    msg = 'Epoch {} of {} done, training PSNR = {:.2f}'.format(a,b,c)
                    print(msg)
                    sys.stdout.flush()
            if i > nsteps: break

    return psnrs


def eval_nl(nl_denoiser,noisy,clean,srch_img,flows,sigma,ws=29,wt=0,
            ensemble=False,verbose=True):
    deno = nl_denoiser(noisy,ensemble=ensemble,flows=flows)
    # deno = nl_denoiser(noisy,sigma,srch_img.clone(),flows=flows,
    #                    rescale=False,ws=ws,wt=wt)
    deno = deno.detach().clamp(-1, 1)
    mse = th.mean((deno / 2-clean / 2)**2).item()
    psnr = -10 * math.log10(mse)
    msg = 'PSNR = {:.2f}'.format(psnr)
    if verbose:
        print(msg)

def get_adapt_dataset(clean,mtype,region_template,nlevels=3):
    rpn = RegionProposalData(clean,mtype,region_template,nlevels)
    return rpn

def add_noise_to_image(clean, noise_sim, sigma):
    if noise_sim is None:
        noisy = clean + sigma_255_to_torch(sigma) * th.randn_like(clean)
    else:
        with th.no_grad():
            noisy = noise_sim(clean)
    return noisy

def sigma_255_to_torch(sigma_255):
    return sigma_255 / 255

def append_adapts_cfg(cfg):
    cfg.lr = 5e-4
    cfg.epochs_between_check = 1
    cfg.block_w = 0
    cfg.dset_stride = 1
    cfg.train_batch_size = 8

# """
# Functions for internal domain adaptation.

# """

# # -- misc --
# import sys,math,gc
# from ..misc import default_options,crop_offset

# # -- data structs --
# import torch.utils.data as data
# from colanet.utils.adapt_data import ImagePairDataSet

# # -- linalg --
# import torch as th
# import numpy as np
# from einops import repeat,rearrange

# # -- path mgmnt --
# from pathlib import Path

# # -- separate class and logic --
# from colanet.utils import clean_code
# from colanet.utils.misc import assert_nonan
# __methods__ = [] # self is a DataStore
# register_method = clean_code.register_method(__methods__)


# # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
# #
# #    Run Adaptation of the Network to Image
# #
# # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

# @register_method
# def run_internal_adapt(self,_noisy,sigma,srch_img=None,flows=None,ws=29,wt=0,
#                        batch_size = -1, nsteps=100, nepochs=5, ensemble=False,
#                        noise_sim = None, verbose=False):
#     if verbose: print("Running Internal Adaptation.")
#     noisy = _noisy/255.
#     opt = default_options()#sigma)
#     append_adapts_cfg(opt)
#     opt.sigma = sigma
#     total_pad = 20
#     nadapts = 1
#     if not(srch_img is None):
#         _srch_img = srch_img/255.
#         _srch_img = _srch_img.contiguous()
#     else: _srch_img = noisy

#     for astep in range(nadapts):
#         with th.no_grad():
#             clean = self(noisy,ensemble=ensemble)
#             # clean = self(noisy,sigma,_srch_img,flows=flows,rescale=False,
#             #              ws=ws,wt=wt,batch_size=batch_size)
#         clean = clean.detach().clamp(0, 1)
#         nl_denoiser = adapt_step(self, clean, _srch_img, flows, opt,
#                                  total_pad, ws=ws, wt=wt, batch_size=batch_size,
#                                  nsteps=nsteps,nepochs=nepochs,
#                                  noise_sim = noise_sim,
#                                  ensemble=ensemble, verbose=verbose)

# @register_method
# def run_external_adapt_og(self,_clean,sigma,srch_img=None,flows=None,ws=29,wt=0,
#                           batch_size = -1, nsteps=100, nepochs=5,
#                           noise_sim=None, verbose=False):

#     if verbose: print("Running External Adaptation.")
#     # -- setup --
#     opt = default_options()#sigma)
#     append_adapts_cfg(opt)
#     opt.sigma = sigma
#     total_pad = 10
#     nadapts = 1
#     clean = (_clean/255. - 0.5)/0.5
#     # -- adapt --
#     if not(srch_img is None):
#         _srch_img = srch_img.contiguous()
#         _srch_img = (_srch_img/255. - 0.5)/0.5
#     else: _srch_img = clean

#     # -- eval before --
#     noisy = add_noise_to_image(clean, noise_sim, opt.sigma)
#     eval_nl(self,noisy,clean,_srch_img,flows,opt.sigma,verbose)

#     for astep in range(nadapts):
#         nl_denoiser = adapt_step(self, clean, _srch_img, flows, opt,
#                                  total_pad, ws=ws,wt=wt,
#                                  noise_sim = noise_sim, batch_size=batch_size,
#                                  nsteps=nsteps,nepochs=nepochs,
#                                  ensemble=ensemble,verbose=verbose)

# def adapt_step(nl_denoiser, clean, srch_img, flows, opt, total_pad,
#                ws=29, wt=0, nsteps=100, nepochs=5, batch_size=-1,
#                ensemble = False, noise_sim = None, verbose=False):

#     # -- optims --
#     criterion = th.nn.MSELoss(reduction='mean')
#     optim = th.optim.Adam(nl_denoiser.parameters(), lr=opt.lr,
#                               betas=(0.9, 0.999), eps=1e-8)
#     # th.autograd.set_detect_anomaly(True)

#     # -- get data --
#     loader,batch_last_it = get_adapt_dataset(clean,srch_img,opt,total_pad)

#     # -- train --
#     noisy = add_noise_to_image(clean, noise_sim, opt.sigma)
#     nl_denoiser.train()

#     # -- epoch --
#     for epoch in range(nepochs):

#         # -- info --
#         if verbose:
#             print('Training epoch {} of {}'.format(epoch + 1, nepochs))

#         # -- garbage collect --
#         sys.stdout.flush()
#         gc.collect()
#         th.cuda.empty_cache()

#         # -- loaders --
#         device = next(nl_denoiser.parameters()).device
#         iloader = enumerate(loader)
#         nsamples = min(len(loader),nsteps)
#         for _i, (clean_i, srch_i) in iloader:

#             # -- update --
#             i = _i + 1

#             # -- tenors on device --
#             srch_i = srch_i.to(device=device).contiguous()
#             clean_i = clean_i.to(device=device).contiguous()
#             noisy_i = add_noise_to_image(clean_i,noise_sim,opt.sigma)
#             noisy_i = noisy_i.contiguous()
#             print("noisy_i.shape: ",noisy_i.shape)

#             # -- forward pass --
#             optim.zero_grad()
#             image_dn = nl_denoiser(noisy_i,ensemble=ensemble)
#             # image_dn = nl_denoiser(noisy_i,opt.sigma,srch_i,flows=flows,
#             #                        ws=ws,wt=wt,train=True,rescale=False,
#             #                        batch_size=batch_size)

#             # -- post-process images --
#             # image_dn = image_dn.clamp(0,1.)
#             total_pad = (clean_i.shape[-1] - image_dn.shape[-1]) // 2
#             image_ref = crop_offset(clean_i, (total_pad,), (total_pad,))

#             # -- compute loss --
#             loss = th.log10(criterion(image_dn, image_ref))
#             assert_nonan(loss)

#             # -- update step --
#             loss.backward()
#             optim.step()

#             if verbose:
#                 print("Processing [%d/%d]: %2.2f" % (i,nsamples,-10*loss.item()))

#             batch_bool = i == batch_last_it
#             epoch_bool = (epoch + 1) % opt.epochs_between_check == 0
#             print_bool = batch_bool and epoch_bool and verbose
#             if print_bool:
#                 gc.collect()
#                 th.cuda.empty_cache()
#                 deno = nl_denoiser(noisy,opt.sigma,srch_img.clone(),flows,
#                                    rescale=False,ws=ws,wt=wt)
#                 deno = deno.detach().clamp(0, 1)
#                 mse = criterion(deno,clean).item()
#                 train_psnr = -10 * math.log10(mse)
#                 a,b,c = epoch + 1, nepochs, train_psnr
#                 msg = 'Epoch {} of {} done, training PSNR = {:.2f}'.format(a,b,c)
#                 print(msg)
#                 sys.stdout.flush()
#             if i >= nsteps: break

#     return nl_denoiser


# def eval_nl(nl_denoiser,noisy,clean,srch_img,flows,sigma,ws=29,wt=0,verbose=True):
#     deno = nl_denoiser(noisy,sigma,srch_img.clone(),flows=flows,
#                        rescale=False,ws=ws,wt=wt)
#     deno = deno.detach().clamp(0, 1)
#     mse = th.mean((deno-clean)**2).item()
#     psnr = -10 * math.log10(mse)
#     msg = 'PSNR = {:.2f}'.format(psnr)
#     if verbose:
#         print(msg)


# def get_adapt_dataset(clean,srch_img,opt,total_pad):

#     # -- prepare data --
#     block_w_pad = opt.block_w + 2 * total_pad
#     ref_img = clean
#     srch_img = srch_img

#     # -- create dataset --
#     dset = ImagePairDataSet(block_w=block_w_pad,
#                             images_a=ref_img, images_b=srch_img,
#                             stride=opt.dset_stride)

#     # -- create loader --
#     loader = data.DataLoader(dset,batch_size=opt.train_batch_size,
#                              shuffle=True, num_workers=0)
#     dlen = loader.dataset.__len__()
#     dbs = loader.batch_size
#     batch_last_it = dlen // dbs - 1
#     return loader,batch_last_it

# def add_noise_to_image(clean, noise_sim, sigma):
#     if noise_sim is None:
#         noisy = clean + sigma_255_to_torch(sigma) * th.randn_like(clean)
#     else:
#         with th.no_grad():
#             noisy = noise_sim(clean)
#     return noisy

# def sigma_255_to_torch(sigma_255):
#     return (sigma_255 / 255)

# def append_adapts_cfg(cfg):
#     cfg.lr = 1e-4
#     cfg.epochs_between_check = 1
#     cfg.block_w = 0
#     cfg.dset_stride = 1
#     cfg.train_batch_size = 8
