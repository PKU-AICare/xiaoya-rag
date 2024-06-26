import os
import re
import json
import tqdm
import torch
import time
import argparse
import transformers
from transformers import AutoTokenizer
import openai
from transformers import StoppingCriteria, StoppingCriteriaList
import tiktoken
import ollama
from utils import RetrievalSystem
from template import *
from config import deep_config, tech_config
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)

config = deep_config
if openai.api_key is None:
    openai.api_type = config["api_type"]
    openai.api_base = config["api_base"] 
    openai.api_key = config["api_key"]


def extract_answer(text):
    pattern_backticks = r'```json(.*?)```'
    match = re.search(pattern_backticks, text, re.DOTALL)
    
    if match:
        json_string = match.group(1).strip()
        return json.loads(json_string)
    
    pattern_json_object = r'\{.*?\}'
    match = re.search(pattern_json_object, text, re.DOTALL)
    if match:
        json_string = match.group(0).strip()
        return json.loads(json_string)

    raise ValueError("No valid JSON content found.")


class RetCare:
    def __init__(self, llm_name="OpenAI/gpt-3.5-turbo-16k", ensemble='select', retriever_name="MedCPT", corpus_name="PubMed", db_dir="./corpus", cache_dir=None):
        self.llm_name = llm_name
        self.ensemble = ensemble
        self.retriever_name = retriever_name
        self.corpus_name = corpus_name
        self.db_dir = db_dir
        self.cache_dir = cache_dir
        self.retrieval_system = RetrievalSystem(self.retriever_name, self.corpus_name, self.db_dir)        
        self.templates = {"ensemble_evaluate_system": ensemble_evaluate_system, "ensemble_evaluate_prompt": ensemble_evaluate_user,
                    "ensemble_select_system": ensemble_select_system, "ensemble_select_prompt": ensemble_select_user}
        if self.llm_name.split('/')[0].lower() == "openai":
            self.model = self.llm_name.split('/')[-1]
            if "gpt-3.5" in self.model or "gpt-35" in self.model:
                self.max_length = 16384
                self.context_length = 15000
            elif "gpt-4" in self.model:
                self.max_length = 32768
                self.context_length = 30000
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
        else:
            self.model = llm_name

    def answer(self, hcontext, keywords=None, k=32, rrf_k=100, save_dir = None):
        '''
        question (str): question to be answered
        options (Dict[str, str]): options to be chosen from
        k (int): number of snippets to retrieve
        save_dir (str): directory to save the results
        '''

        # retrieve relevant snippets using keywords or question
        if keywords is not None:
            retrieved_snippets, scores = self.retrieval_system.retrieve(keywords, k=k, rrf_k=rrf_k)
        else:
            retrieved_snippets, scores = self.retrieval_system.retrieve(hcontext, k=k, rrf_k=rrf_k)
        contexts = ["Document [{:d}] (Title: {:s}) {:s}".format(idx, retrieved_snippets[idx]["title"], retrieved_snippets[idx]["content"]) for idx in range(len(retrieved_snippets))]
        if len(contexts) == 0:
            contexts = [""]
        if "openai" in self.llm_name.lower():
            context = self.tokenizer.decode(self.tokenizer.encode("\n".join(contexts))[:self.context_length])
        # else:
        #     contexts = [self.tokenizer.decode(self.tokenizer.encode("\n".join(contexts), add_special_tokens=False)[:self.context_length])]

        if save_dir is not None and not os.path.exists(save_dir):
            os.makedirs(save_dir)

        # generate answer
        if self.ensemble == 'select':
            prompt_user = self.templates["ensemble_select_prompt"].render(context=context, hcontext=hcontext)
            prompt_system = self.templates["ensemble_select_system"]
        elif self.ensemble == 'evaluate':
            prompt_user = self.templates["ensemble_prompt"].render(context=context, hcontext=hcontext)
            prompt_system = self.templates["ensemble_system"]
        messages=[
                {"role": "system", "content": prompt_system},
                {"role": "user", "content": prompt_user}
        ]
        ans = self.generate(messages)
        ans = extract_answer(re.sub("\s+", " ", ans))

        if save_dir is not None:
            with open(os.path.join(save_dir, "snippets.json"), 'w') as f:
                json.dump(retrieved_snippets, f, indent=4)
            with open(os.path.join(save_dir, "response.json"), 'w') as f:
                json.dump(ans, f, indent=4)

        return ans, retrieved_snippets, scores, messages

    # @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
    def generate(self, messages):
        '''
        generate response given messages
        '''
        if "openai" in self.llm_name.lower():
            if openai.api_type == "azure":
                response = openai.ChatCompletion.create(
                    engine=self.model,
                    messages=messages,
                    temperature=0.0,
                )
            else:
                response = openai.ChatCompletion.create(
                    model="deepseek-chat",
                    messages=messages,
                )
            ans = response["choices"][0]["message"]["content"]
        elif "llama" in self.llm_name.lower():
            response = ollama.chat(
                model=self.llm_name,
                messages=messages,
            )
            ans = response["message"]["content"]
        return ans