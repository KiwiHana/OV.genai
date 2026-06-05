# OV.genai
## the following command test all txt in prompts and save result in csv 
python ov_run_benchmark_linux.py -pf "C:\Users\3\Documents\prompts" -m C:\Users\3\Documents\Qwen3.6-35B-A3B-ov -raw Qwen3.6-35B-A3B-ov-325.csv


## the following command only test 32k in， 512 out
python benchmark_vlm_new.py -pf "C:\Users\3\Documents\9204_opt\prompts-qwen3.6-35b\prompt_6_32Kin_512out_r1.txt" -m C:\Users\3\Documents\Qwen3.5-4B-ov-int4 -d GPU -n 3 -mt 512
