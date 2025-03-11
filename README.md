# HEFFLPNet

## A transformer based unet for prostate segmentation on (micro-)us dataset 

### weight: https://drive.google.com/file/d/169jUNV_3BUKcnURf8yhSvCVbnEAzjQio/view?usp=drive_link

### Usage:refer to: https://github.com/mirthAI/MicroSegNet
download data at https://github.com/mirthAI/MicroSegNet and https://github.com/S-domain/Multi-Stage-FCN
download Google pre-trained ViT models at https://console.cloud.google.com/storage/browser/vit_models;tab=objects?pli=1&prefix=&forceOnObjectsSortingFiltering=false
put micro-US and cch-thrusps dataset in 

```plaintext
data
├── Micro_Ultrasound_Prostate_Segmentation_Dataset
│   ├── train
│   ├── test
│   │   ├── ...
│   │   ├── cch_image
│   │   ├── cch_mask
│   ├── preprocessing.py
│
├── model
│   ├── vit_checkpoint
│   │   ├── imagenet21k
│   │   │   ├── R50+ViT-B_16.npz
│   │   │   ├── *.npz
│
├── TransUNet

run python preprocessing.py for micro-US dataset

and then:
python train_MicroUS.py
python test_MicroUS.py


### REF:https://github.com/mirthAI/MicroSegNet
