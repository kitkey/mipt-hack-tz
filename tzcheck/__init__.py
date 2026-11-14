import os

# LiteLLM при первом вызове скачивает карту цен моделей с GitHub; проверка ТЗ без сети берёт встроенную копию
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
