import random
import os
import PIL
import torch
import warnings
from typing import List

warnings.filterwarnings("ignore")

from transformers import set_seed
from tqdm import tqdm
from transformers import logging
from diffusers import ControlNetModel, StableDiffusionControlNetImg2ImgPipeline, DDIMScheduler
from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection
from torchvision import transforms
from safetensors import safe_open
from torch.cuda.amp import autocast

import torch.nn as nn
import numpy as np
from PIL import Image
from collections import OrderedDict

import utils.feature_utils as fu
import utils.preprocesser_utils as pu
import utils.image_process_utils as ipu

from .utils import is_torch2_available, Embedding_Adapter, ImageProjModel

if is_torch2_available():
    from .attention_processor import (
        AttnProcessor2_0 as AttnProcessor,
    )
    from .attention_processor import (
        CNAttnProcessor2_0 as CNAttnProcessor,
    )
    from .attention_processor import (
        IPAttnProcessor2_0 as IPAttnProcessor,
    )
else:
    from .attention_processor import AttnProcessor, CNAttnProcessor, IPAttnProcessor

logging.set_verbosity_error()

def set_seed_lib(seed):
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    random.seed(seed)
    set_seed(seed)

@torch.no_grad()
class IPA_RAVE_MultiControlNet(nn.Module):
    def __init__(self, device, num_tokens=4):
        super().__init__()
        self.device = device
        self.dtype = torch.float
        self.num_tokens = num_tokens

    @torch.no_grad()
    def __init_pipe(self, hf_cn_path, hf_path):
        controlnet_1 = ControlNetModel.from_pretrained(hf_cn_path[0], torch_dtype=self.dtype).to(self.device, self.dtype)
        controlnet_2 = ControlNetModel.from_pretrained(hf_cn_path[1], torch_dtype=self.dtype).to(self.device, self.dtype)
        pipe = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(hf_path, controlnet=[controlnet_1, controlnet_2], torch_dtype=self.dtype, local_files_only=True).to(self.device, self.dtype) 
        pipe.enable_model_cpu_offload()
        pipe.enable_xformers_memory_efficient_attention()
        return pipe
        
    @torch.no_grad()
    def init_models(self, hf_cn_path, hf_path, preprocess_name, image_encoder_path, ip_ckpt, model_id=None):
        if model_id is None or model_id == "None":
            pipe = self.__init_pipe(hf_cn_path, hf_path)  
        else:
            pipe = self.__init_pipe(hf_cn_path, model_id)  
        self.preprocess_name_1, self.preprocess_name_2 = preprocess_name.split('-')
        
        self.vae = pipe.vae
        self.unet = pipe.unet

        self.controlnet = pipe.controlnet
        self.scheduler_config = pipe.scheduler.config        
        
        self.set_ip_adapter()
        self._prepare_control_image = pipe.prepare_control_image
        self.run_safety_checker = pipe.run_safety_checker
        
        self.tokenizer = pipe.tokenizer
        self.text_encoder = pipe.text_encoder
        
        # self.adapter = None
        # adapter_chkpt = 'pretrained_models/adapter_499.pth'
        # adapter_state_dict = torch.load(adapter_chkpt)
        # new_state_dict = OrderedDict()
        # for k, v in adapter_state_dict.items():
        #     name = k.replace('module.', '')  #name = k[7:] if k[:7] == 'module' else k 
        #     new_state_dict[name] = v
        # pipe.adapter = Embedding_Adapter()
        # pipe.adapter.load_state_dict(new_state_dict)
        # self.adapter = pipe.adapter.cuda()

        
        self.image_encoder = CLIPVisionModelWithProjection.from_pretrained(image_encoder_path).to(
            self.device, dtype=torch.float
        )
        self.clip_image_processor = CLIPImageProcessor()
        self.image_proj_model = self.init_proj()
        self.load_ip_adapter(ip_ckpt)

        del pipe
                
    def init_proj(self):
        image_proj_model = ImageProjModel(
            cross_attention_dim=self.unet.config.cross_attention_dim,
            clip_embeddings_dim=self.image_encoder.config.projection_dim,
            clip_extra_context_tokens=self.num_tokens,
        ).to(self.device, dtype=torch.float)
        return image_proj_model

    def set_ip_adapter(self):
        attn_procs = {}
        for name in self.unet.attn_processors.keys():
            cross_attention_dim = None if name.endswith("attn1.processor") else self.unet.config.cross_attention_dim
            if name.startswith("mid_block"):
                hidden_size = self.unet.config.block_out_channels[-1]
            elif name.startswith("up_blocks"):
                block_id = int(name[len("up_blocks.")])
                hidden_size = list(reversed(self.unet.config.block_out_channels))[block_id]
            elif name.startswith("down_blocks"):
                block_id = int(name[len("down_blocks.")])
                hidden_size = self.unet.config.block_out_channels[block_id]
            if cross_attention_dim is None:
                attn_procs[name] = AttnProcessor()
            else:
                attn_procs[name] = IPAttnProcessor(
                    hidden_size=hidden_size,
                    cross_attention_dim=cross_attention_dim,
                    scale=1.0,
                    num_tokens=self.num_tokens,
                ).to(self.device, dtype=torch.float)
        self.unet.set_attn_processor(attn_procs)
        for controlnet in self.controlnet.nets:
            controlnet.set_attn_processor(CNAttnProcessor(num_tokens=self.num_tokens))

    def load_ip_adapter(self, ip_ckpt):
        if os.path.splitext(ip_ckpt)[-1] == ".safetensors":
            state_dict = {"image_proj": {}, "ip_adapter": {}}
            with safe_open(ip_ckpt, framework="pt", device=self.device) as f:
                for key in f.keys():
                    if key.startswith("image_proj."):
                        state_dict["image_proj"][key.replace("image_proj.", "")] = f.get_tensor(key)
                    elif key.startswith("ip_adapter."):
                        state_dict["ip_adapter"][key.replace("ip_adapter.", "")] = f.get_tensor(key)
        else:
            state_dict = torch.load(ip_ckpt, map_location=self.device)
        self.image_proj_model.load_state_dict(state_dict["image_proj"])
        ip_layers = torch.nn.ModuleList(self.unet.attn_processors.values())
        ip_layers.load_state_dict(state_dict["ip_adapter"])

    @torch.no_grad()
    def get_image_encoded(self, image, num_images_per_prompt=1):
        image = [image]
        uncond_image = [torch.zeros((image[0].size[0], image[0].size[1], 3))]

        with autocast():
            # clip encoder
            inputs = self.clip_image_processor(images=image, return_tensors="pt").pixel_values
            image_embeddings = self.image_encoder(inputs.to(self.device, dtype=torch.float)).image_embeds
            uncond_inputs = self.clip_image_processor(images=uncond_image, return_tensors="pt").pixel_values
            uncond_image_embeddings = self.image_encoder(uncond_inputs.to(self.device, dtype=torch.float)).image_embeds
            
            image_embeddings = self.image_proj_model(image_embeddings)
            uncond_image_embeddings = self.image_proj_model(uncond_image_embeddings)

        bs_embed, seq_len, _ = image_embeddings.shape
        image_embeddings  = image_embeddings.repeat(1, num_images_per_prompt, 1)
        image_embeddings = image_embeddings.view(bs_embed * num_images_per_prompt, seq_len, -1)

        bs_embed, seq_len, _ = uncond_image_embeddings.shape
        uncond_image_embeddings  = uncond_image_embeddings.repeat(1, num_images_per_prompt, 1)
        uncond_image_embeddings = uncond_image_embeddings.view(bs_embed * num_images_per_prompt, seq_len, -1)

        return image_embeddings, uncond_image_embeddings

    def set_scale(self, scale):
        for attn_processor in self.unet.attn_processors.values():
            if isinstance(attn_processor, IPAttnProcessor):
                attn_processor.scale = scale

    @torch.no_grad()
    def get_text_embeds(self, prompt, negative_prompt):
        cond_input = self.tokenizer(prompt, padding='max_length', max_length=self.tokenizer.model_max_length, truncation=True, return_tensors='pt')
        cond_embeddings = self.text_encoder(cond_input.input_ids.to(self.device))[0]
        # Do the same for unconditional embeddings
        uncond_input = self.tokenizer(negative_prompt, padding='max_length', max_length=self.tokenizer.model_max_length, return_tensors='pt')
        uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]
        return cond_embeddings, uncond_embeddings

    @torch.no_grad()
    def prepare_control_image(self, control_pil, width, height):
        control_image = self._prepare_control_image(
        image=control_pil,
        width=width,
        height=height,
        device=self.device,
        dtype=self.controlnet.dtype,
        batch_size=1,
        num_images_per_prompt=1
    )
        return control_image
    
    @torch.no_grad()
    def pred_controlnet_sampling(self, current_sampling_percent, latent_model_input, t, prompt_embeddings, control_image):
        if (current_sampling_percent < self.controlnet_guidance_start or current_sampling_percent > self.controlnet_guidance_end):
            down_block_res_samples = None
            mid_block_res_sample = None
        else:       
            down_block_res_samples, mid_block_res_sample = self.controlnet(
                latent_model_input,
                t,
                conditioning_scale=self.controlnet_conditioning_scale,
                encoder_hidden_states=prompt_embeddings,
                controlnet_cond=control_image,
                return_dict=False,
            )
        noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=prompt_embeddings,                    
                            down_block_additional_residuals=down_block_res_samples,
                            mid_block_additional_residual=mid_block_res_sample)['sample']
        return noise_pred
    
    
    @torch.no_grad()
    def denoising_step(self, latents, control_image_1, control_image_2, prompt_embeddings, t, guidance_scale, current_sampling_percent):
        # expand the latents if we are doing classifier-free guidance to avoid doing two forward passes.
        
        latent_model_input = torch.cat([latents] * 2)
        control_image_1 = torch.cat([control_image_1] * 2)
        control_image_2 = torch.cat([control_image_2] * 2)
        latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)
        # compute the percentage of total steps we are at
        noise_pred = self.pred_controlnet_sampling(current_sampling_percent, latent_model_input, t, prompt_embeddings, [control_image_1, control_image_2])
        # perform guidance
        noise_pred_uncond, noise_pred_prompt = noise_pred.chunk(2)
        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_prompt - noise_pred_uncond)
        # compute the previous noisy sample x_t -> x_t-1
        latents = self.scheduler.step(noise_pred, t, latents)['prev_sample']
        return latents

    @torch.no_grad()
    def image_prompt_process(self, image_prompt_pil):
        depth_map = pu.pixel_perfect_process(np.array(image_prompt_pil, dtype='uint8'), self.preprocess_name_1)
        depth_img = PIL.Image.fromarray(depth_map).convert("L")
        return(depth_img)
    
    @torch.no_grad()
    def preprocess_control_grid(self, image_pil):
        list_of_image_pils = fu.pil_grid_to_frames(image_pil, grid_size=self.grid) # List[C, W, H] -> len = num_frames
        list_of_pils_1, list_of_pils_2 = [], []
        for frame_pil in list_of_image_pils:
            frame_pil_1 = pu.pixel_perfect_process(np.array(frame_pil, dtype='uint8'), self.preprocess_name_1)
            frame_pil_2 = pu.pixel_perfect_process(np.array(frame_pil, dtype='uint8'), self.preprocess_name_2)
            list_of_pils_1.append(frame_pil_1)
            list_of_pils_2.append(frame_pil_2)
        control_images_1 = np.array(list_of_pils_1, dtype='uint8')
        control_images_2 = np.array(list_of_pils_2, dtype='uint8')
        
        control_img_1 = ipu.create_grid_from_numpy(control_images_1, grid_size=self.grid)
        control_img_1 = PIL.Image.fromarray(control_img_1.astype(np.uint8)).convert("L")
        
        control_img_2 = ipu.create_grid_from_numpy(control_images_2, grid_size=self.grid)
        control_img_2 = PIL.Image.fromarray(control_img_2.astype(np.uint8))

        return control_img_1, control_img_2
    
    @torch.no_grad()
    def shuffle_latents(self, latents, control_image_1, control_image_2, indices):
        rand_i = torch.randperm(self.total_frame_number).tolist()
        # latents, _ = fu.prepare_key_grid_latents(latents, self.grid, self.grid, rand_i)
        # control_image, _ = fu.prepare_key_grid_latents(control_image, self.grid, self.grid, rand_i)
        
        latents_l, controls_l_1, controls_l_2, randx = [], [], [], []
        for j in range(self.sample_size):
            rand_indices = rand_i[j*self.grid_frame_number:(j+1)*self.grid_frame_number]

            latents_keyframe, _ = fu.prepare_key_grid_latents(latents, self.grid, self.grid, rand_indices)
            control_keyframe_1, _ = fu.prepare_key_grid_latents(control_image_1, self.grid, self.grid, rand_indices)
            control_keyframe_2, _ = fu.prepare_key_grid_latents(control_image_2, self.grid, self.grid, rand_indices)
            latents_l.append(latents_keyframe)
            controls_l_1.append(control_keyframe_1)
            controls_l_2.append(control_keyframe_2)
            randx.extend(rand_indices)
        rand_i = randx.copy()
        latents = torch.cat(latents_l, dim=0)
        control_image_1 = torch.cat(controls_l_1, dim=0)
        control_image_2 = torch.cat(controls_l_2, dim=0)
        indices = [indices[i] for i in rand_i]
        return latents, indices, control_image_1, control_image_2
    
    @torch.no_grad()
    def batch_denoise(self, latents, control_image_1, control_image_2, indices, t, guidance_scale, current_sampling_percent):
        latents_l, controls_l_1, controls_l_2 = [], [], []
        control_split_1 = control_image_1.split(self.batch_size, dim=0)
        control_split_2 = control_image_2.split(self.batch_size, dim=0)
        latents_split = latents.split(self.batch_size, dim=0)
        
        for idx in range(len(control_split_1)):
            txt_embed = torch.cat([self.uncond_embeddings] * len(latents_split[idx]) + [self.cond_embeddings] * len(latents_split[idx])) 
            latents = self.denoising_step(latents_split[idx], control_split_1[idx], control_split_2[idx], txt_embed, t, guidance_scale, current_sampling_percent)    
            latents_l.append(latents)
            controls_l_1.append(control_split_1[idx])
            controls_l_2.append(control_split_2[idx])
            
        latents = torch.cat(latents_l, dim=0)
        controls_1 = torch.cat(controls_l_1, dim=0)
        controls_2 = torch.cat(controls_l_2, dim=0)
        return latents, indices, controls_1, controls_2
    
    @torch.no_grad()
    def reverse_diffusion(self, latents=None, control_image_1=None, control_image_2=None, guidance_scale=7.5, indices=None):
        self.scheduler.set_timesteps(self.num_inference_steps, device=self.device)
        with torch.autocast("cuda"):
            for i, t in tqdm(enumerate(self.scheduler.timesteps), desc='reverse_diffusion'):
                indices = list(indices)
                current_sampling_percent = i / len(self.scheduler.timesteps)
                
                if self.is_shuffle:
                    latents, indices, control_image_1, control_image_2 = self.shuffle_latents(latents, control_image_1, control_image_2, indices)
                if self.cond_step_start < current_sampling_percent:
                    latents, indices, control_image_1, control_image_2 = self.batch_denoise(latents, control_image_1, control_image_2, indices, t, guidance_scale, current_sampling_percent)
                else:
                    latents, indices, control_image_1, control_image_2 = self.batch_denoise(latents, control_image_1, control_image_2, indices, t, 0.0, current_sampling_percent)
        return latents, indices, control_image_1, control_image_2

    @torch.no_grad()
    def encode_imgs(self, img_torch):
        latents_l = []
        splits = img_torch.split(self.batch_size_vae, dim=0)
        for split in splits:
            image = 2 * split - 1
            posterior = self.vae.encode(image).latent_dist
            latents = posterior.mean * self.vae.config.scaling_factor
            latents_l.append(latents)
        return torch.cat(latents_l, dim=0)

    @torch.no_grad()
    def decode_latents(self, latents):
        image_l = []
        splits = latents.split(self.batch_size_vae, dim=0)
        for split in splits:
            image = self.vae.decode(split / self.vae.config.scaling_factor, return_dict=False)[0]
            image = (image / 2 + 0.5).clamp(0, 1)
            image_l.append(image)
        return torch.cat(image_l, dim=0)


    @torch.no_grad()
    def controlnet_pred(self, latent_model_input, t, prompt_embed_input, controlnet_cond):
        down_block_res_samples, mid_block_res_sample = self.controlnet(
            latent_model_input,
            t,
            encoder_hidden_states=prompt_embed_input,
            controlnet_cond=controlnet_cond,
            conditioning_scale=self.controlnet_conditioning_scale,
            return_dict=False,
        )
        
        # apply the denoising network
        noise_pred = self.unet(
            latent_model_input,
            t,
            encoder_hidden_states=prompt_embed_input,
            cross_attention_kwargs={},
            down_block_additional_residuals=down_block_res_samples,
            mid_block_additional_residual=mid_block_res_sample,
            return_dict=False,
        )[0]
        return noise_pred

    @torch.no_grad()
    def ddim_inversion(self, latents, control_batch_1, control_batch_2, indices):
        k = None
        els = os.listdir(self.inverse_path) 
        els = [el for el in els if el.endswith('.pt')]
        for k,inv_path in enumerate(sorted(els, key=lambda x: int(x.split('.')[0]))):
            latents[k] = torch.load(os.path.join(self.inverse_path, inv_path)).to(device=self.device)

        self.inverse_scheduler = DDIMScheduler.from_config(self.scheduler_config)
        self.inverse_scheduler.set_timesteps(self.num_inversion_step, device=self.device)
        self.timesteps = reversed(self.inverse_scheduler.timesteps)

        if k == (latents.shape[0]-1):
            return latents, indices, control_batch_1, control_batch_2
        inv_cond = torch.cat([self.inv_uncond_embeddings] * 1 + [self.inv_cond_embeddings] * 1)[1].unsqueeze(0)
        for i, t in enumerate(tqdm(self.timesteps)):
            alpha_prod_t = self.inverse_scheduler.alphas_cumprod[t]
            alpha_prod_t_prev = (self.inverse_scheduler.alphas_cumprod[self.timesteps[i - 1]] if i > 0 else self.inverse_scheduler.final_alpha_cumprod)
            
            # latent, indices, control_batch = self.shuffle_latents(latent, control_batch, indices)
            latents_l = [] 
            latents_split = latents.split(self.batch_size, dim=0)
            control_batch_split_1 = control_batch_1.split(self.batch_size, dim=0)
            control_batch_split_2 = control_batch_2.split(self.batch_size, dim=0)
            for idx in range(len(latents_split)):
                cond_batch = inv_cond.repeat(latents_split[idx].shape[0], 1, 1)
                # print(cond_batch.shape, latents_split[idx].shape, control_batch_split_1[idx].shape, control_batch_split_2[idx].shape)
                # input()
                latents = self.ddim_step(latents_split[idx], t, cond_batch, alpha_prod_t, alpha_prod_t_prev, control_batch_split_1[idx], control_batch_split_2[idx])
                latents_l.append(latents)
            latents = torch.cat(latents_l, dim=0)
        for k,i in enumerate(latents):
            torch.save(i.detach().cpu(), f'{self.inverse_path}/{str(k).zfill(5)}.pt')        
        return latents, indices, control_batch_1, control_batch_2
    
   
    def ddim_step(self, latent_frames, t, cond_batch, alpha_prod_t, alpha_prod_t_prev, control_batch_1, control_batch_2):
        mu = alpha_prod_t ** 0.5
        mu_prev = alpha_prod_t_prev ** 0.5
        sigma = (1 - alpha_prod_t) ** 0.5
        sigma_prev = (1 - alpha_prod_t_prev) ** 0.5
        if self.give_control_inversion:
            eps = self.controlnet_pred(latent_frames, t, prompt_embed_input=cond_batch, controlnet_cond=[control_batch_1, control_batch_2])
        else:
            eps = self.unet(latent_frames, t, encoder_hidden_states=cond_batch, return_dict=False)[0]
        pred_x0 = (latent_frames - sigma_prev * eps) / mu_prev
        latent_frames = mu * pred_x0 + sigma * eps
        return latent_frames
    

    def process_image_batch(self, image_pil_list):
        if len(os.listdir(self.controls_path)) > 0:
            control_torch_1 = torch.load(os.path.join(self.controls_path, 'control_1.pt')).to(self.device)
            control_torch_2 = torch.load(os.path.join(self.controls_path, 'control_2.pt')).to(self.device)
            img_torch = torch.load(os.path.join(self.controls_path, 'img.pt')).to(self.device)
        else:
            image_torch_list = []
            control_torch_list_1, control_torch_list_2 = [], []
            for image_pil in image_pil_list:
                width, height = image_pil.size
                # control_pil = PIL.Image.fromarray(pu.pixel_perfect_process(np.array(image_pil, dtype='uint8'), self.preprocess_name))
                control_pil_1, control_pil_2 = self.preprocess_control_grid(image_pil)
                control_image_1 = self.prepare_control_image(control_pil_1, width, height)
                control_image_2 = self.prepare_control_image(control_pil_2, width, height)
                
                control_torch_list_1.append(control_image_1)
                control_torch_list_2.append(control_image_2)
                image_torch_list.append(ipu.pil_img_to_torch_tensor(image_pil))
            control_torch_1 = torch.cat(control_torch_list_1, dim=0).to(self.device)
            control_torch_2 = torch.cat(control_torch_list_2, dim=0).to(self.device)
            img_torch = torch.cat(image_torch_list, dim=0).to(self.device)
            torch.save(control_torch_1, os.path.join(self.controls_path, 'control_1.pt'))
            torch.save(control_torch_2, os.path.join(self.controls_path, 'control_2.pt'))
            torch.save(img_torch, os.path.join(self.controls_path, 'img.pt'))           
        return img_torch, control_torch_1, control_torch_2
        
    def order_grids(self, list_of_pils, indices):
        k = []
        for i in range(len(list_of_pils)):
            k.extend(fu.pil_grid_to_frames(list_of_pils[i], self.grid))
        frames = [k[indices.index(i)] for i in np.arange(len(indices))]    
        return frames

    @torch.no_grad()
    def __preprocess_inversion_input(self, init_latents, control_batch_1, control_batch_2):
        list_of_flattens = [fu.flatten_grid(el.unsqueeze(0), self.grid) for el in init_latents]
        init_latents = torch.cat(list_of_flattens, dim=-1)
        init_latents = torch.cat(torch.chunk(init_latents, self.total_frame_number, dim=-1), dim=0)
        
        control_batch_flattens_1 = [fu.flatten_grid(el.unsqueeze(0), self.grid) for el in control_batch_1]
        control_batch_1 = torch.cat(control_batch_flattens_1, dim=-1)
        control_batch_1 = torch.cat(torch.chunk(control_batch_1, self.total_frame_number, dim=-1), dim=0)
        
        control_batch_flattens_2 = [fu.flatten_grid(el.unsqueeze(0), self.grid) for el in control_batch_2]
        control_batch_2 = torch.cat(control_batch_flattens_2, dim=-1)
        control_batch_2 = torch.cat(torch.chunk(control_batch_2, self.total_frame_number, dim=-1), dim=0)
        
        return init_latents, control_batch_1, control_batch_2
    
    
    @torch.no_grad()
    def __postprocess_inversion_input(self, latents_inverted, control_batch_1, control_batch_2):
            latents_inverted = torch.cat([fu.unflatten_grid(torch.cat([a for a in latents_inverted[i*self.grid_frame_number:(i+1)*self.grid_frame_number]], dim=-1).unsqueeze(0), self.grid) for i in range(self.sample_size)] , dim=0)
            control_batch_1 = torch.cat([fu.unflatten_grid(torch.cat([a for a in control_batch_1[i*self.grid_frame_number:(i+1)*self.grid_frame_number]], dim=-1).unsqueeze(0), self.grid) for i in range(self.sample_size)] , dim=0)
            control_batch_2 = torch.cat([fu.unflatten_grid(torch.cat([a for a in control_batch_2[i*self.grid_frame_number:(i+1)*self.grid_frame_number]], dim=-1).unsqueeze(0), self.grid) for i in range(self.sample_size)] , dim=0)
            return latents_inverted, control_batch_1, control_batch_2
        
    
    
    
    @torch.no_grad()
    def __call__(self, input_dict):
        set_seed_lib(input_dict['seed'])
        
        self.grid_size = input_dict['grid_size']
        self.sample_size = input_dict['sample_size']
        
        self.grid_frame_number = self.grid_size * self.grid_size
        self.total_frame_number = (self.grid_frame_number) * self.sample_size
        self.grid = [self.grid_size, self.grid_size]
        
        self.cond_step_start = input_dict['cond_step_start']
        
        self.controlnet_guidance_start = input_dict['controlnet_guidance_start']
        self.controlnet_guidance_end = input_dict['controlnet_guidance_end']
        self.controlnet_conditioning_scale = [float(x) for x in input_dict['controlnet_conditioning_scale'].split('-')]
        
        self.inversion_prompt = input_dict['inversion_prompt']
        
        self.batch_size = input_dict['batch_size']
        self.batch_size_vae = input_dict['batch_size_vae']

        self.num_inference_steps = input_dict['num_inference_steps']
        self.num_inversion_step = input_dict['num_inversion_step']
        self.inverse_path = input_dict['inverse_path']
        self.controls_path = input_dict['control_path']
        
        self.image_path = input_dict['image_path']
        self.clip_image_embeds = input_dict['clip_embeds']
        
        self.is_ddim_inversion = input_dict['is_ddim_inversion']

        self.is_shuffle = input_dict['is_shuffle']
        self.give_control_inversion = input_dict['give_control_inversion']

        self.guidance_scale = input_dict['guidance_scale']
        
        indices = list(np.arange(self.total_frame_number))
        
        img_prompt = Image.open(self.image_path)
        pil_img_prompt = img_prompt.resize((512, 640)) 
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5])
        ])
        img_prompt_tensor = transform(pil_img_prompt).unsqueeze(0)  # Add batch dimension
        img_prompt_tensor = img_prompt_tensor.to(self.device)   
                
        img_batch, control_batch_1, control_batch_2 = self.process_image_batch(input_dict['image_pil_list'])
        init_latents_pre = self.encode_imgs(img_batch)
        
        self.scheduler = DDIMScheduler.from_config(self.scheduler_config)
        self.scheduler.set_timesteps(self.num_inference_steps, device=self.device)
        self.inv_cond_embeddings, self.inv_uncond_embeddings = self.get_text_embeds(self.inversion_prompt, "")
        if self.is_ddim_inversion:
            init_latents, control_batch_1, control_batch_2 = self.__preprocess_inversion_input(init_latents_pre, control_batch_1, control_batch_2)
            latents_inverted, indices, control_batch_1, control_batch_2 = self.ddim_inversion(init_latents, control_batch_1, control_batch_2, indices)
            latents_inverted, control_batch_1, control_batch_2 = self.__postprocess_inversion_input(latents_inverted, control_batch_1, control_batch_2)
        else:
            init_latents_pre = torch.cat([init_latents_pre], dim=0) 
            noise = torch.randn_like(init_latents_pre)
            latents_inverted = self.scheduler.add_noise(init_latents_pre, noise, self.scheduler.timesteps[:1])

        prompt = "best quality, high quality, realisitic, smooth human"
        negative_prompt = "monochrome, lowres, bad anatomy, worst quality, low quality"    
        image_prompt_embeds, uncond_image_prompt_embeds = self.get_image_encoded(pil_img_prompt) 
        with torch.no_grad():
            prompt_embeds_, negative_prompt_embeds_ = self.get_text_embeds(
                prompt,
                negative_prompt=negative_prompt,
            )
            self.cond_embeddings = torch.cat([prompt_embeds_, image_prompt_embeds], dim=1)
            self.uncond_embeddings = torch.cat([negative_prompt_embeds_, uncond_image_prompt_embeds], dim=1)

        latents_denoised, indices, controls_1, controls_2 = self.reverse_diffusion(latents_inverted, control_batch_1, control_batch_2, self.guidance_scale, indices=indices)
    
        image_torch = self.decode_latents(latents_denoised)
        ordered_img_frames = self.order_grids(ipu.torch_to_pil_img_batch(image_torch), indices)
        ordered_control_frames_1 = self.order_grids(ipu.torch_to_pil_img_batch(controls_1), indices)
        ordered_control_frames_2 = self.order_grids(ipu.torch_to_pil_img_batch(controls_2), indices)
        return ordered_img_frames, ordered_control_frames_1, ordered_control_frames_2
    
