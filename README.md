# HEFFLPNet

## Intro: 
A transformer-based unet for prostate segmentation on (micro-)us dataset 

### 1. Title: 
Hierarchically Enhanced Feature Fusion and Loss Prevention for Prostate Segmentation on Micro-Ultrasound Images
#### 2. Description
Three novel modules for feature fusion
- Tri-Cross Attention with Feature Enhancement (TriCAFE)
- Multi-Scale Prediction Map Attention (MSPMA)
- Upsample Fusion Attention (UpFA)

### weight: https://drive.google.com/file/d/169jUNV_3BUKcnURf8yhSvCVbnEAzjQio/view?usp=drive_link

### 3. Dataset info:
Two datasets of prostate used: An ultrasound dataset and a micro-ultrasound dataset.

download micro-ultrasound dataset at https://github.com/mirthAI/MicroSegNet 

and ultrasound dataset source is at paper: Multi-stage fully convolutional network for precise prostate segmentation in ultrasound images

### 4. Code information
This project is on the basis of MicroSegNet: https://github.com/mirthAI/MicroSegNet 
Our new modules can be found in HEFFLPNet/TransUNet/networks/vit_seg_modeling.py

### 5. Steps for implementation:
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

### 6. Requirements
Required dependencies can be found in requirements.txt

### 7. Methodology (Steps to preprocess data)
run python preprocessing.py to preprocess micro-US dataset

and then:
```plaintext
python train_MicroUS.py
python test_MicroUS.py
```


### REF:https://github.com/mirthAI/MicroSegNet
