export PRETRAINED_MODEL_DIR=facebook/bart-large
export TRAINED_MODEL_DIR=trained_models/


QUERY=$1

python -m semantic_parsing_with_constrained_lm.run_instant \
--config-name semantic_parsing_with_constrained_lm.configs.liveRun \
--log-dir logs/ \
--model Bart \
--utterance "$QUERY" \
--exp-name-pattern 'overnight_Bart_test-full_.*_constrained_canonicalUtterance_train-200' 

# QUERY=$1
# TESTCASE="{\"canonical\": \"default\", \"formula\": \"default\", \"natural\": \"${QUERY}\"}"
# echo ${TESTCASE} > 'src/semantic_parsing_with_constrained_lm/domains/overnight/data/foodshed.test.jsonl'
# domain="foodshed"

# python -m semantic_parsing_with_constrained_lm.run_exp \
# --config-name semantic_parsing_with_constrained_lm.configs.overnight_emnlp_camera_ready \
# --log-dir logs/ \
# --model Bart \
# --eval-split test-full \
# --exp-name-pattern 'overnight_Bart_test-full_.*_constrained_canonicalUtterance_train-200' 

# python3 demo.py "${QUERY}" || > tempLog.txt
# poetry run python -m semantic_parsing_with_constrained_lm.finetune.lm_finetune \
#         --exp-names overnight_${domain}_utterance \
#         --lr 1e-6 \
#         --num-steps 40000 \
#         --steps-per-save 5000 \
#         --model-type BartV3 \
#         --steps-per-decay 8 \
#         --batch-size 32
        
# echo "Finished tuning utterances"

# poetry run python -m semantic_parsing_with_constrained_lm.finetune.lm_finetune \
#         --exp-names overnight_${domain}_meaningRepresentation \
#         --lr 1e-5 \
#         --num-steps 20000 \
#         --steps-per-save 5000 \
#         --model-type BartV3 \
#         --steps-per-decay 8 \
#         --batch-size 32

# echo "Finished tuning meaning representations"