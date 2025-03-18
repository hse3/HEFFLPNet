# HEFFLPNet

## Intro: 
## A transformer based unet for prostate segmentation on (micro-)us dataset 
### Title: Hierarchically Enhanced Feature Fusion and Loss Prevention for Prostate Segmentation on Micro-Ultrasound Images
#### Three novel modules for feature fusion:
- Tri-Cross Attention with Feature Enhancement (TriCAFE)
- Multi-Scale Prediction Map Attention (MSPMA)
- Upsample Fusion Attention (UpFA)

### weight: https://drive.google.com/file/d/169jUNV_3BUKcnURf8yhSvCVbnEAzjQio/view?usp=drive_link

### Dataset info:
Two datasets of prostate used: An ultrasound dataset and a micro-ultrasound dataset.

download micro-ultrasound dataset at https://github.com/mirthAI/MicroSegNet 

and ultrasound dataset at https://github.com/S-domain/Multi-Stage-FCN

### Steps for implementation:
download Google pre-trained ViT models at https://console.cloud.google.com/storage/browser/vit_models;tab=objects?pli=1&prefix=&forceOnObjectsSortingFiltering=false

put micro-US and cch-thrusps dataset in 

```plaintext
├── data
│   ├── Micro_Ultrasound_Prostate_Segmentation_Dataset
│   │   ├── train
│   │	  └── test
│   │     ├── ...
│   │     ├── cch_image
│   │     ├── cch_mask
│   └── preprocessing.py
│
├── model
│   ├── vit_checkpoint
│   │   ├── imagenet21k
│   │   │   ├── R50+ViT-B_16.npz
│   │   │   ├── *.npz
│
├── TransUNet
```
run python preprocessing.py to preprocess micro-US dataset

and then:
```plaintext
python train_MicroUS.py
python test_MicroUS.py
```


### REF:https://github.com/mirthAI/MicroSegNet
