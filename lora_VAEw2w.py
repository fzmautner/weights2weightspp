import os
import math
from typing import Optional, List, Type, Set, Literal

import torch
import torch.nn as nn
from diffusers import UNet2DConditionModel
from safetensors.torch import save_file



UNET_TARGET_REPLACE_MODULE_TRANSFORMER = [
#     "Transformer2DModel",  # どうやらこっちの方らしい？ # attn1, 2
    "Attention"
]
UNET_TARGET_REPLACE_MODULE_CONV = [
    "ResnetBlock2D",
    "Downsample2D",
    "Upsample2D",
    "DownBlock2D",
    "UpBlock2D",
    
]  # locon, 3clier

LORA_PREFIX_UNET = "lora_unet"

DEFAULT_TARGET_REPLACE = UNET_TARGET_REPLACE_MODULE_TRANSFORMER

TRAINING_METHODS = Literal[
    "noxattn",  # train all layers except x-attns and time_embed layers
    "innoxattn",  # train all layers except self attention layers
    "selfattn",  # ESD-u, train only self attention layers
    "xattn",  # ESD-x, train only x attention layers
    "full",  #  train all layers
    "xattn-strict", # q and k values
    "noxattn-hspace",
    "noxattn-hspace-last",
    # "xlayer",
    # "outxattn",
    # "outsattn",
    # "inxattn",
    # "inmidsattn",
    # "selflayer",
]

class LoRAVAEModule(nn.Module):
    """
    applies LoRA updates using parameters decoded from a VAE latent.
    Replaces forward pass of original module while maintaining a reference to it.
    """
    def __init__(self, lora_name, param_slices, org_module, multiplier=1.0, rank=4, alpha=1):
        super().__init__()
        self.lora_name = lora_name
        self.multiplier = multiplier
        self.org_module = org_module
        
        # Store dimensions for parameter reshaping
        self.slice_A, self.slice_B = param_slices
        
        # Alpha scaling as in original LoRA
        self.rank = rank
        alpha = rank if alpha is None or alpha == 0 else alpha
        self.scale = alpha / rank
        
        # Save original module and its forward pass
        self.org_module = org_module
        self.org_forward = None
        self.params = None  # Will hold our portion of decoded parameters

    def apply_to(self):
        # Called once during initialization.
        self.org_forward = self.org_module.forward
        self.org_module.forward = self.forward
        del self.org_module  # Remove reference but keep forward pass

    def forward(self, x):
        if self.multiplier == 0 or self.params is None:
            return self.org_forward(x)
            
        params_A = self.params[self.slice_A]
        params_B = self.params[self.slice_B]
        
        lora_A = params_A.reshape(1, -1)
        lora_B = params_B.reshape(-1, 1)
        
        return self.org_forward(x) + (x @ lora_A.T @ lora_B.T) * self.multiplier * self.scale


class LoRAw2wVAE(nn.Module):
    """
    Manages LoRA modules that use parameters generated by a VAE decoder.
    Only the latent vector z is optimized during inversion.
    """
    def __init__(self, vae, unet, rank=4, multiplier=1.0, alpha=1.0, train_method="full"):
        super().__init__()
        self.vae = vae
        self.multiplier = multiplier
        self.lora_dim = rank
        self.alpha = alpha

        self.weight_dimensions = torch.load('../files/weight_dimensions.pt')
        
        # Learnable latent vector
        self.z = nn.Parameter(torch.zeros(vae.latent_dim))
        
        # Create and apply LoRA modules
        # self.unet_loras = self.create_modules(
        #     LORA_PREFIX_UNET,
        #     unet,
        #     DEFAULT_TARGET_REPLACE,
        #     self.lora_dim,
        #     self.multiplier,
        #     train_method=train_method
        # )
        self.unet_loras = self.create_modules(unet, train_method)
        
        # Apply modules to replace forward passes
        for lora in self.unet_loras:
            lora.apply_to()

    def _should_skip_module(self, name, train_method):
        """Determine if a module should be skipped based on training method."""
        if train_method == "noxattn" or train_method == "noxattn-hspace" or train_method == "noxattn-hspace-last":
            if "attn2" in name or "time_embed" in name:
                return True
        elif train_method == "innoxattn":
            if "attn2" in name:
                return True
        elif train_method == "selfattn":
            if "attn1" not in name:
                return True
        elif train_method == "xattn" or train_method == "xattn-strict":
            if "to_k" in name:
                return True
            if train_method == "xattn-strict" and ("out" in name or "to_k" in name):
                return True
        elif train_method == "full":
            return False
        else:
            raise NotImplementedError(f"train_method: {train_method} is not implemented.")
        
        return False

    # very similar to the one in LoRAw2w: DOES NOT WORK BC ORDER OF PARAMS IS DEF WRONG
    # def create_modules(self, prefix, root_module, target_replace_modules, rank, multiplier, train_method):
    #     """Create LoRA modules for target layers and assign parameter slices."""
    #     counter = 0
    #     loras = []
        
    #     # Get all modules and names
    #     mm = []
    #     nn = []
    #     for name, module in root_module.named_modules():
    #         nn.append(name)
    #         mm.append(module)
        
    #     # Reorder blocks as in original implementation
    #     midstart = next(i for i, name in enumerate(nn) if "mid_block" in name)
    #     upstart = next(i for i, name in enumerate(nn) if "up_block" in name)
        
    #     mm = mm[:upstart] + mm[midstart:] + mm[upstart:midstart]
    #     nn = nn[:upstart] + nn[midstart:] + nn[upstart:midstart]
        
    #     for i in range(len(mm)):
    #         name = nn[i]
    #         module = mm[i]
            
    #         if self._should_skip_module(name, train_method):
    #             continue
                
    #         if module.__class__.__name__ in target_replace_modules:
    #             for child_name, child_module in module.named_modules():
    #                 if child_module.__class__.__name__ in ["Linear", "Conv2d"]:
    #                     lora_name = prefix + "." + name + "." + child_name
    #                     lora_name = lora_name.replace(".", "_")
                        
    #                     # Calculate slice for this module's parameters
    #                     params_size = child_module.in_features + child_module.out_features

    #                     print(f"Module {lora_name}:")
    #                     print(f"in_features: {child_module.in_features}")
    #                     print(f"out_features: {child_module.out_features}")
    #                     print(f"params_size: {params_size}")
    #                     print(f"counter before: {counter}")

    #                     slice_indices = slice(counter, counter + params_size)
    #                     counter += params_size
                        
    #                     lora = LoRAVAEModule(
    #                         lora_name,
    #                         slice_indices,
    #                         child_module,
    #                         multiplier,
    #                         rank,
    #                         self.alpha
    #                     )
    #                     loras.append(lora)

    #     assert counter == 99648, f"Parameter count mismatch! Got {counter}, expected 99648"
    #     return loras
    def create_modules(self, unet, train_method):
        loras = []
        counter = 0
        
        # This is basically the same as unflatten
        module_pairs = {}
        for key in self.weight_dimensions.keys():
            # Convert the key from lora_unet format to base_model.model format
            diffusers_key = key.replace("lora_unet_", "base_model.model.")\
                            .replace("A", "down")\
                            .replace("B", "up")\
                            .replace("weight", "identity1.weight")\
                            .replace("_lora", ".lora")\
                            .replace("lora_down", "lora_A")\
                            .replace("lora_up", "lora_B")
                            
            # without the lora_A/lora_B suffix: weigths_dict splits them already but we
            # need just the whole module
            base_key = diffusers_key.rsplit('.lora_', 1)[0]
            
            if base_key not in module_pairs:
                module_pairs[base_key] = {"A": None, "B": None}
                
            if "lora_A" in diffusers_key:
                module_pairs[base_key]["A"] = (key, diffusers_key)
            else:
                module_pairs[base_key]["B"] = (key, diffusers_key)
        
        # process each module
        for base_key, pair in module_pairs.items():
            orig_key_A, diffusers_key_A = pair["A"]
            orig_key_B, diffusers_key_B = pair["B"]
            
            # UNet module path
            module_path = base_key.replace("base_model.model.", "")
            
            # this should never be called
            if self._should_skip_module(module_path, train_method):
                print("called oops")
                continue
            
            # A and B matrix sizes
            size_A = self.weight_dimensions[orig_key_A][0][0]
            size_B = self.weight_dimensions[orig_key_B][0][0]
            
            # their slices into the unflattened weights
            slice_A = slice(counter, counter + size_A)
            counter += size_A
            slice_B = slice(counter, counter + size_B)
            counter += size_B
            
            # get module in UNet
            module = unet
            for part in module_path.split('.'):
                module = getattr(module, part)
            
            # make a LoRA module for it
            lora = LoRAVAEModule(
                base_key,  # use the diffusers-style name
                (slice_A, slice_B),
                module,
                self.multiplier,
                self.lora_dim,
                self.alpha
            )
            loras.append(lora)

        print("total params counted:", counter)
        return loras
    
    def __enter__(self):
        """Activate LoRA modules with freshly decoded parameters."""
        params = self.vae.decode(self.z).bfloat16()
        
        # Distribute parameters and activate modules
        for lora in self.unet_loras:
            lora.params = params  # Each module will use its slice
            lora.multiplier = self.multiplier

    def __exit__(self, exc_type, exc_value, tb):
        """Deactivate LoRA modules."""
        for lora in self.unet_loras:
            lora.multiplier = 0
            lora.params = None

    def parameters(self):
        yield self.z