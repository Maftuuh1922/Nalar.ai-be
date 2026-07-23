import os
from knowledge_storm import STORMWikiRunnerArguments, STORMWikiRunner, STORMWikiLMConfigs
from knowledge_storm.rm import DuckDuckGoSearchRM
from knowledge_storm.lm import OpenAIModel
from app.core.config import settings

def setup_storm_runner(output_dir: str = "storm_output") -> STORMWikiRunner:
    """Sets up the STORM wiki runner with the AI model configuration."""
    # Ensure output dir exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    lm_configs = STORMWikiLMConfigs()
    
    # We use our OpenAI-compatible base URL.
    # Storm expects api_key and api_base in kwargs for OpenAIModel
    openai_kwargs = {
        "api_key": "mock", # LiteLLM local API key or any dummy string if not needed
        "api_base": settings.LLM_BASE_URL,
    }
    
    model_name = settings.LLM_MODEL
    
    # STORM uses multiple LLMs for different tasks. We map them all to the user's model.
    conv_simulator_lm = OpenAIModel(model=model_name, max_tokens=500, **openai_kwargs)
    question_asker_lm = OpenAIModel(model=model_name, max_tokens=500, **openai_kwargs)
    outline_gen_lm = OpenAIModel(model=model_name, max_tokens=400, **openai_kwargs)
    article_gen_lm = OpenAIModel(model=model_name, max_tokens=700, **openai_kwargs)
    article_polish_lm = OpenAIModel(model=model_name, max_tokens=4000, **openai_kwargs)

    lm_configs.set_conv_simulator_lm(conv_simulator_lm)
    lm_configs.set_question_asker_lm(question_asker_lm)
    lm_configs.set_outline_gen_lm(outline_gen_lm)
    lm_configs.set_article_gen_lm(article_gen_lm)
    lm_configs.set_article_polish_lm(article_polish_lm)

    # Initialize DuckDuckGo RM. k=3 is the number of snippets per query.
    rm = DuckDuckGoSearchRM(k=3, safe_search="Off", region="wt-wt")

    engine_args = STORMWikiRunnerArguments(output_dir=output_dir)
    runner = STORMWikiRunner(engine_args, lm_configs, rm)
    
    return runner
