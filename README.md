# HEFFLPNet

## A transformer based unet for prostate segmentation on (micro-)us dataset 

### weight: https://drive.google.com/file/d/169jUNV_3BUKcnURf8yhSvCVbnEAzjQio/view?usp=drive_link

### Usage:refer to: https://github.com/mirthAI/MicroSegNet
download data at https://github.com/mirthAI/MicroSegNet and https://github.com/S-domain/Multi-Stage-FCN
download Google pre-trained ViT models at https://console.cloud.google.com/storage/browser/vit_models;tab=objects?pli=1&prefix=&forceOnObjectsSortingFiltering=false
put micro-US and cch-thrusps dataset in 

- `data/` - Contains the dataset for micro-ultrasound prostate segmentation.
  - `train/` - Training dataset.
  - `test/` - Testing dataset.
    - `cch_image/` - Contains test images.
    - `cch_mask/` - Contains test masks.
  - `preprocessing.py` - Script for preprocessing the dataset.

- `model/` - Stores model checkpoints.
  - `vit_checkpoint/` - Vision Transformer (ViT) pre-trained checkpoints.
  - `imagenet21k/` - Pre-trained ImageNet21k model weights.
  - `R50+ViT-B_16.npz` - ResNet50 + ViT Base 16 model weights.
  - `*.npz` - Other ViT model checkpoints.

- `TransUNet/` - Contains the **TransUNet** model implementation.

run python preprocessing.py for micro-US dataset

and then:
python train_MicroUS.py
python test_MicroUS.py


### REF:https://github.com/mirthAI/MicroSegNet
