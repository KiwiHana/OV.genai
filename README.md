# OV.genai
```
conda create -n ov python=3.12

conda activate ov

conda install pip

python -m pip install -r requirements.txt
```
Download model from: https://huggingface.co/OpenVINO/Qwen3.6-35B-A3B-int4-ov

## the following command test all txt in prompts and save result in csv on Windows
python ov_run_benchmark_vlm_win.py -pf ./prompts -m C:\Documents\Qwen3.6-35B-A3B-ov -raw Qwen3.6-35B-A3B-ov-325.csv


## the following command test all txt in prompts and save result in csv on Ubuntu
python ov_run_benchmark_linux.py -pf ./prompts -m Qwen3.6-35B-A3B-ov -raw Qwen3.6-35B-A3B-ov-325.csv


## the following command only test 2k in， 512 out
python benchmark_vlm_new.py -pf .\prompts\prompt_2_2Kin_512out_r1.txt -m Qwen3.5-4B-ov-int4 -d GPU -n 3 -mt 512
