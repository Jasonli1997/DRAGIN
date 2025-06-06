import numpy as np
import logging
import spacy
import torch
from math import exp
from scipy.special import softmax
from .retriever import BM25, SGPT, DatabricksVectorSearch
from transformers import AutoTokenizer, AutoModelForCausalLM, AutoConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

nlp = spacy.load("en_core_web_sm")


class BasicGenerator:
    def __init__(self, model_name_or_path):
        logger.info(f"Loading model from {model_name_or_path}")
        self.tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
        self.model_config = AutoConfig.from_pretrained(
            model_name_or_path, trust_remote_code="falcon" in model_name_or_path
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name_or_path,
            device_map="auto",
            torch_dtype="auto",
            attn_implementation="eager",
            trust_remote_code="falcon" in model_name_or_path,
        )
        if self.model_config.model_type == "llama":
            self.space_token = "▁"
        else:
            self.space_token = self.tokenizer.tokenize(" ")[0]
        self.eos_token = self.tokenizer.special_tokens_map["eos_token"]
        self.newline_token = self.tokenizer.encode("\n")[-1]

        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

    def generate(
        self, input_text, max_length, enable_thinking=False, return_logprobs=False
    ):
        messages = [{"role": "user", "content": input_text}]
        text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=enable_thinking,  # Switches between thinking and non-thinking modes
        )
        model_inputs = self.tokenizer([text], return_tensors="pt").to(self.model.device)
        input_length = model_inputs["input_ids"].shape[1]

        if return_logprobs:
            outputs = self.model.generate(
                **model_inputs,
                max_new_tokens=max_length,
                return_dict_in_generate=True,
                output_scores=True,
            )
            transition_scores = self.model.compute_transition_scores(
                outputs.sequences, outputs.scores, normalize_logits=True
            )

            generated_ids = outputs.sequences[:, input_length:]
            text = self.tokenizer.decode(generated_ids[0])
            tokens = [self.tokenizer.decode(t) for t in generated_ids[0]]
            logprobs = transition_scores[0]
            logprobs = [p.cpu().numpy() for p in logprobs]
            assert len(tokens) == len(logprobs)
            return text, generated_ids, logprobs, outputs

        else:
            outputs = self.model.generate(
                **model_inputs,
                max_new_tokens=max_length,
            )
            generated_ids = outputs[0][input_length:].tolist()
            # parsing thinking content
            try:
                # rindex finding 151668 (</think>)
                # TODO: make this non-Qwen centric
                index = len(generated_ids) - generated_ids[::-1].index(151668)
            except ValueError:
                index = 0

            thinking_content = self.tokenizer.decode(
                generated_ids[:index], skip_special_tokens=True
            ).strip("\n")
            if thinking_content:
                logger.info(f"Reasoning: {thinking_content}")
            text = self.tokenizer.decode(
                generated_ids[index:], skip_special_tokens=True
            ).strip("\n")
            return text, None, None, None
    
    def generate_attn(self, input_text, max_length, solver="max", use_entropy = False, use_logprob = False):
        # TODO: can enable_thinking be True in this case?
        text, generated_ids, logprobs, outputs = self.generate(input_text=input_text,
                                                               max_length=max_length,
                                                               return_logprobs=True)
        # merge tokens
        range_ = []
        tokens = self.tokenizer.convert_ids_to_tokens(generated_ids[0])
        tokens = [t.replace("Ċ", "\n") for t in tokens]
        for i, t in enumerate(tokens):
            if i == 0 or t.startswith(self.space_token) or generated_ids[0][i] == self.newline_token or tokens[i-1] == self.eos_token:
                range_.append([i, i])
            else:
                range_[-1][-1] += 1

        # attention
        atten = self.model(generated_ids, output_attentions=True).attentions[-1][0]
        if solver == "max": 
            mean_atten, _ = torch.max(atten, dim=1)
            mean_atten = torch.mean(mean_atten, dim=0)  # multi-head attn
        elif solver == "avg":
            mean_atten = torch.sum(atten, dim=1)
            mean_atten = torch.mean(mean_atten, dim=0)
            for i in range(mean_atten.shape[0]):
                mean_atten[i] /= (mean_atten.shape[0] - i)
        elif solver == "last_token":
            mean_atten = torch.mean(atten[:, -1], dim=0)
        else:
            raise NotImplementedError
        if mean_atten.shape[0] > 1 and tokens[0] == self.eos_token:
            mean_atten = mean_atten / sum(mean_atten[1:]).item()
        # mean_atten = mean_atten[tl:tr]
            
        # regular tokens
        seqlist = []
        attns = []
        for r in range_:
            tokenseq = "".join(tokens[r[0]: r[1]+1]).replace(self.space_token, "")
            value = sum(mean_atten[r[0]: r[1]+1]).item()
            seqlist.append(tokenseq)
            attns.append(value)

        # -log prob
        if use_logprob:
            seqlogprobs = []
            for r in range_:
                logprobseq = sum(logprobs[r[0]:r[1]+1]) / (r[1] - r[0] + 1)
                seqlogprobs.append(logprobseq)
        else:
            seqlogprobs = None

        # entropy
        if use_entropy:
            tmp = []
            for v in scores:
                tmp.append(v.cpu())
            softmax_probs = softmax(tmp, axis=-1)
            entropies = -np.sum(softmax_probs * np.log(softmax_probs + 1e-10), axis=-1)
            entropies = [v[0] for v in entropies]
            seqentropies = []
            for r in range_:
                entropyseq = sum(entropies[r[0]:r[1]+1]) / (r[1] - r[0] + 1)
                seqentropies.append(entropyseq) 
        else:
            seqentropies = None 

        return text, seqlist, attns, seqlogprobs, seqentropies


class Counter:
    def __init__(self):
        self.retrieve = 0
        self.generate = 0
        self.hallucinated = 0
        self.token = 0
        self.sentence = 0

    def add_generate(self, text, tokenizer):
        self.generate += 1
        ids = tokenizer(text, return_tensors="pt")['input_ids'][0].tolist()
        self.token += len(ids)
        sentences = [sent.text for sent in nlp(text).sents]
        self.sentence += len(sentences)

    def calc(self, other_counter):
        return {
            "retrieve_count": self.retrieve - other_counter.retrieve, 
            "generate_count": self.generate - other_counter.generate,
            "hallucinated_count": self.hallucinated - other_counter.hallucinated, 
            "token_count": self.token - other_counter.token, 
            "sentence_count": self.sentence - other_counter.sentence 
        }


class BasicRAG:
    def __init__(self, args):
        if hasattr(args, "__dict__"):
            args = args.__dict__
        for k, v in args.items():
            setattr(self, k, v)
        self.generator = BasicGenerator(self.model_name_or_path)
        if "retriever_configs" in self.__dict__:
            self.retrievers = {}
            self.multi_vector_index = (
                False if len(self.retriever_configs) == 1 else True
            )
            print(f"Enable multi-vector index: {self.multi_vector_index}")

            for config in self.retriever_configs:
                retriever_name = list(config.keys())[0]
                config_dict = config[retriever_name]
                retriever_type, description = config_dict.pop("retriever_type"), config_dict.pop("description")
                retriever = self._retriever_selector(retriever_type, **config_dict)

                self.retrievers[retriever_name] = {"description": description,
                                                   "retriever": retriever}

        self.counter = Counter()

    def retrieve(self, query, topk=1, max_query_length=64):
        self.counter.retrieve += 1
        docs = None
        if not self.multi_vector_index:
            retriever_name = list(self.retrievers.keys())[0]
            docs = self._retrieve(query, self.retrievers[retriever_name]['retriever'], topk, max_query_length)

        else:
            # generator decides which index to choose
            prompt = "Return which vector indices to select based on the query and index descriptions.\n"
            prompt += f"User query: {query}\n"
            prompt += "Vector Index Summary:\n"
            for k, v in self.retrievers.items():
                prompt += f"- {k}: {v['description']}\n"

            prompt += "Output ONLY your index selection(s) in bullet points ('-'), separated by a '\\n':"
            model_output = self.generator.generate(prompt, max_length=64)[0].strip()
            print(
                f"Index selected -> {model_output}"
            )
            docs = []
            for ret in model_output.split("\n"):
                docs.extend(self._retrieve(query, self.retrievers[ret.split("-")[-1].strip()]['retriever'], topk, max_query_length))

        return docs

    def _retrieve(self, query, retriever, topk=1, max_query_length=64):
        self.counter.retrieve += 1
        retriever_type = retriever.__class__.__name__
        if retriever_type == "BM25":
            _docs_ids, docs = retriever.retrieve(
                queries=[query],
                topk=topk,
                max_query_length=max_query_length,
            )
        elif retriever_type == "SGPT":
            docs = retriever.retrieve(
                queries=[query],
                topk=topk,
            )
        elif retriever_type == "DatabricksVectorSearch":
            docs = retriever.retrieve(
                queries=[query],
                columns=["text", "filename"],
                topk=topk,
            )
        else:
            raise NotImplementedError(f"{retriever_type} not supported...")

        return docs[0]

    def _retriever_selector(self, retriever_type, **kwargs):
        if retriever_type == "BM25":
            # gpt2_tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")
            retriever = BM25(
                tokenizer=self.generator.tokenizer,
                engine="elasticsearch",
                **kwargs,
            )
        elif retriever_type == "SGPT":
            retriever = SGPT(**kwargs)

        elif retriever_type == "DatabricksVectorSearch":
            retriever = DatabricksVectorSearch(**kwargs)

        else:
            return NotImplementedError("Retriever not supported")

        return retriever

    def get_top_sentence(self, text):
        sentences = [sent.text.strip() for sent in nlp(text).sents]
        sentences = [sent for sent in sentences if len(sent) > 0]
        return sentences[0] if len(sentences) > 0 else ""

    def get_last_sentence(self, text):
        sentences = [sent.text.strip() for sent in nlp(text).sents]
        sentences = [sent for sent in sentences if len(sent) > 0]
        return sentences[-1] if len(sentences) > 0 else ""
    
    def decompose_query(self, question):
        prompt = "Decompose the user query below into sub-queries if the question contains multiple parts or comparisons. Leave it unchanged otherwsie. Output the subqueries or original question in bullet points ('-') separated by '\\n'.\n"
        prompt += """### Example 1:
User Query:
How does attention work in transformers, and how is it different from RNNs?

Decomposed Output:
- How does attention work in transformers?
- How is attention different from RNNs?

### Example 2:
User Query:
Compare the effectiveness of FiD, RAG-Token, and DRAGIN for long-context QA.

Decomposed Output:
- How effective is FiD for long-context QA?
- How effective is RAG-Token for long-context QA?
- How effective is DRAGIN for long-context QA?

### Example 3:
User Query:
What are the benefits of using vector databases in RAG pipelines?

Decomposed Output:
- What are the benefits of using vector databases in RAG pipelines?"
"""
        prompt += f"\nUser Query: {question}\nDecomposed Output:"
        print("***Query Decomposition Prompt***\n", prompt)
        text, _, _, _ = self.generator.generate(
            input_text=prompt,
            max_length=self.generate_max_length,
            enable_thinking=self.enable_thinking,
        )
        return text.split("\n")

    def inference(self, question, demo, case):
        # non-retrieval
        assert self.query_formulation == "direct"
        prompt = "".join([d["case"] + "\n" for d in demo])
        prompt += case
        text, _, _, _ = self.generator.generate(prompt, self.generate_max_length)
        if self.use_counter == True:
            self.counter.add_generate(text, self.generator.tokenizer)
        return text


class SingleRAG(BasicRAG):
    def __init__(self, args):
        super().__init__(args)

    def inference(self, question, demo, case):
        assert self.query_formulation == "direct"
        if self.query_decomposition == True:
            subqueries = self.decompose_query(question)
            print(f"Questions list: {subqueries}")
            docs = []
            for subquery in subqueries:
                docs.extend(self.retrieve(subquery.strip(), topk=self.retrieve_topk))

        else:
            docs = self.retrieve(question, topk=self.retrieve_topk)
        # 对 topk 个 passage 生成 prompt
        prompt = "".join([d["case"] + "\n" for d in demo])
        prompt += "Context:\n"
        for i, doc in enumerate(docs):
            prompt += f"[{i+1}] {doc}\n"
        prompt += "Answer the user query below ONLY using the context above and admit that you don't know if there is no relevant context.\n"
        prompt += f"**********\nUser Query: {question}\n**********"
        # TODO: what is case from example datasets
        prompt += case
        print("***Inference Prompt***", prompt)
        text, _, _, _ = self.generator.generate(
            input_text=prompt,
            max_length=self.generate_max_length,
            enable_thinking=enable_thinking,
        )
        
        if self.use_counter == True:
            self.counter.add_generate(text, self.generator.tokenizer)
        return text


class FixLengthRAG(BasicRAG):
    def __init__(self, args):
        super().__init__(args)

    def inference(self, question, demo, case):
        assert self.query_formulation == "direct"
        text = ""
        retrieve_question = question
        while True:
            old_len = len(text)
            docs = self.retrieve(retrieve_question, topk=self.retrieve_topk)
            prompt = "".join([d["case"] + "\n" for d in demo])
            prompt += "Context:\n"
            for i, doc in enumerate(docs):
                prompt += f"[{i+1}] {doc}\n"
            prompt += "Answer in t he same format as before.\n"
            prompt += case + " " + text
            if self.method == "fix-length-retrieval":
                new_text, _, _ = self.generator.generate(prompt, self.fix_length)
                if self.use_counter == True:
                    self.counter.add_generate(new_text, self.generator.tokenizer)
                text = text.strip() + " " + new_text.strip()
                retrieve_question = new_text.strip()
            else:
                # fix sentence
                new_text, _, _ = self.generator.generate(
                    prompt, self.generate_max_length
                )
                if self.use_counter == True:
                    self.counter.add_generate(new_text, self.generator.tokenizer)
                new_text = new_text.strip()
                sentences = list(nlp(new_text).sents)
                sentences = [str(sent).strip() for sent in sentences]
                if len(sentences) == 0:
                    break
                text = text.strip() + " " + str(sentences[0])
                retrieve_question = sentences[0]

            # 判断 token 的个数要少于 generate_max_length
            tokens_count = len(self.generator.tokenizer.encode(text))
            if (
                tokens_count > self.generate_max_length
                or len(text) <= old_len
                or "the answer is" in text
            ):
                break
        return text


class TokenRAG(BasicRAG):
    def __init__(self, args):
        super().__init__(args)

    def modifier(self, text, tokens, logprobs):
        sentences = [sent.text.strip() for sent in nlp(text).sents]
        sentences = [sent for sent in sentences if len(sent) > 0]

        tid = 0
        for sid, sent in enumerate(sentences):
            pos = 0
            tr = tid
            while tr < len(tokens):
                apr = sent[pos:].find(tokens[tr])
                if apr == -1:
                    break
                pos = apr + len(tokens[tr])
                tr += 1
            probs = [1 - exp(v) for v in logprobs[tid : tr + 1]]
            probs = np.array(probs)
            p = {
                "avg": np.mean,
                "max": np.max,
                "min": np.min,
            }.get(
                self.sentence_solver, lambda x: 0
            )(probs)
            if p > self.hallucination_threshold:  # hallucination
                # keep sentences before hallucination
                prev = "" if sid == 0 else " ".join(sentences[:sid])
                # replace all hallucinated tokens in current sentence with [xxx]
                curr = sentences[sid]
                pos = 0
                # # 这里改成了替换掉最大的那个，而不是所有的
                # max_prob = 0
                # for prob, tok in zip(probs, tokens[tid:tr+1]):
                #     max_prob = max(prob, max_prob)
                for prob, tok in zip(probs, tokens[tid : tr + 1]):
                    apr = curr[pos:].find(tok) + pos
                    if prob > self.hallucination_threshold:
                        # if prob == max_prob:
                        curr = curr[:apr] + "[xxx]" + curr[apr + len(tok) :]
                        pos = apr + len("[xxx]")
                    else:
                        pos = apr + len(tok)
                return prev, curr, True
            tid = tr + 1

        # No hallucination
        return text, None, False

    def inference(self, question, demo, case):
        # assert self.query_formulation == "direct"
        text = ""
        while True:
            old_len = len(text)
            prompt = "".join([d["case"] + "\n" for d in demo])
            prompt += case + " " + text
            new_text, tokens, logprobs = self.generator.generate(
                prompt, self.generate_max_length, return_logprobs=True
            )
            if self.use_counter == True:
                self.counter.add_generate(new_text, self.generator.tokenizer)
            ptext, curr, hallucination = self.modifier(new_text, tokens, logprobs)
            if not hallucination:
                text = text.strip() + " " + new_text.strip()
            else:
                if self.query_formulation == "direct":
                    retrieve_question = curr.replace("[xxx]", "")
                elif self.query_formulation == "forward_all":
                    tmp_all = [question, text, ptext]
                    retrieve_question = " ".join(s for s in tmp_all if len(s) > 0)
                else:
                    raise NotImplemented

                docs = self.retrieve(retrieve_question, topk=self.retrieve_topk)
                prompt = "".join([d["case"] + "\n" for d in demo])
                prompt += "Context:\n"
                for i, doc in enumerate(docs):
                    prompt += f"[{i+1}] {doc}\n"
                prompt += "Answer in the same format as before.\n"
                prompt += case + " " + text + " " + ptext.strip()
                new_text, _, _ = self.generator.generate(
                    prompt, self.generate_max_length
                )
                if self.use_counter == True:
                    self.counter.add_generate(new_text, self.generator.tokenizer)
                    self.counter.hallucinated += 1
                text = text.strip() + " " + ptext.strip() + " " + new_text.strip()

            # 判断 token 的个数要少于 generate_max_length
            tokens_count = len(self.generator.tokenizer.encode(text))
            if (
                tokens_count > self.generate_max_length
                or len(text) <= old_len
                or "the answer is" in text
            ):
                break
        return text


class EntityRAG(TokenRAG):
    def __init__(self, args):
        super().__init__(args)

    def modifier(self, text, tokens, logprobs):
        sentences = [sent.text.strip() for sent in nlp(text).sents]
        sentences = [sent for sent in sentences if len(sent) > 0]

        entity = []
        for sent in sentences:
            doc = nlp(sent)
            li = [ent.text for ent in doc.ents]
            entity.append(li)

        belonging = [-1] * len(text)
        pos = 0
        for tid, tok in enumerate(tokens):
            apr = text[pos:].find(tok) + pos
            assert apr != -1
            for j in range(pos, apr + len(tok)):
                belonging[j] = tid
            pos = apr + len(tok)

        entity_intv = []
        for sid, sent in enumerate(sentences):
            tmp = []
            pos = text.find(sent)
            for ent in entity[sid]:
                apr = text[pos:].find(ent) + pos
                el = belonging[apr]
                er = belonging[apr + len(ent) - 1]
                tmp.append((el, er))
                pos = apr + len(ent)
            entity_intv.append(tmp)

        entity_prob = []
        for ent_itv_per_sent in entity_intv:
            tmp = []
            for itv in ent_itv_per_sent:
                probs = np.array(logprobs[itv[0] : itv[1] + 1])
                p = {
                    "avg": np.mean,
                    "max": np.max,
                    "min": np.min,
                    "first": lambda x: x[0] if len(x) > 0 else 0,
                }.get(self.entity_solver, lambda x: 0)(probs)
                tmp.append(p)
            entity_prob.append(tmp)

        for sid in range(len(sentences)):
            if len(entity_prob[sid]) == 0:
                continue
            probs = [1 - exp(v) for v in entity_prob[sid]]
            probs = np.array(probs)
            p = {
                "avg": np.mean,
                "max": np.max,
                "min": np.min,
            }.get(
                self.sentence_solver, lambda x: 0
            )(probs)
            if p > self.hallucination_threshold:  # hallucination
                # keep sentences before hallucination
                prev = "" if sid == 0 else " ".join(sentences[:sid])
                # replace all hallucinated entities in current sentence with [xxx]
                curr = sentences[sid]
                pos = 0
                for prob, ent in zip(probs, entity[sid]):
                    apr = curr[pos:].find(ent) + pos
                    if prob > self.hallucination_threshold:
                        curr = curr[:apr] + "[xxx]" + curr[apr + len(ent) :]
                        pos = apr + len("[xxx]")
                    else:
                        pos = apr + len(ent)
                return prev, curr, True
        # No hallucination
        return text, None, False

    def inference(self, question, demo, case):
        return super().inference(question, demo, case)


class AttnWeightRAG(BasicRAG):
    def __init__(self, args):
        super().__init__(args)

    def modifier(self, text, tokens, attentions, weight):
        sentences = [sent.text for sent in nlp(text).sents]
        sentences = [sent for sent in sentences if len(sent) > 0]
        tid = 0
        for sid, sent in enumerate(sentences):
            tl, tr = tid, tid
            if sid == len(sentences) - 1:
                tl, tr = tid, len(tokens)
            else:
                for i in range(tid + 1, len(tokens)):
                    seq = " ".join(tokens[tl:i])
                    if sent in seq:
                        tr = i
                        break
                tid = tr
            # value = attenion * (-log prob)
            attns = attentions[tl:tr]
            attns = np.array(attns) / sum(attns)
            value = [attns[i - tl] * weight[i] * (tr - tl) for i in range(tl, tr)]
            thres = [1 if v > self.hallucination_threshold else 0 for v in value]
            if 1 in thres:
                print(f"Hallucination detected in '{sent}")
                # hallucinated
                if "check_real_words" in self.__dict__ and self.check_real_words:
                    doc = nlp(sent)
                    real_words = set(
                        token.text
                        for token in doc
                        if token.pos_ in ["NOUN", "ADJ", "VERB", "PROPN", "NUM"]
                    )

                    def match(tok):
                        for word in real_words:
                            if word in tok:
                                return True
                        return False

                    for i in range(len(thres)):
                        if not match(tokens[tl + i]):
                            thres[i] = 0

                prev = "" if sid == 0 else " ".join(sentences[:sid])
                # curr = " ".join(
                #     [tokens[i] if thres[i] == 0 else "[xxx]" for i in range(len(thres))]
                # )
                return True, prev, tokens[tl:tr], thres
        return False, text, None, None

    def keep_real_words(self, prev_text, curr_tokens, curr_hit):
        curr_text = " ".join(curr_tokens)
        all_text = prev_text + " " + curr_text
        input_ids = self.generator.tokenizer.encode(all_text, return_tensors="pt")
        input_length = input_ids.shape[1]
        tokens_tmp = self.generator.tokenizer.convert_ids_to_tokens(input_ids[0])

        atten_tmp = self.generator.model(input_ids, output_attentions=True).attentions[-1][0]

        # merge tokens
        range_ = []
        for i, t in enumerate(tokens_tmp):
            if (
                i == 0
                or t.startswith(self.generator.space_token)
                or input_ids[0][i] == self.generator.newline_token
            ):
                range_.append([i, i])
            else:
                range_[-1][-1] += 1
        tokens = []
        for r in range_:
            tokenseq = "".join(tokens_tmp[r[0] : r[1] + 1]).replace(
                self.generator.space_token, ""
            )
            tokens.append(tokenseq)

        # 获取幻觉词对应的 attention
        curr_st = len(tokens) - len(curr_tokens)
        atten_tmp = torch.mean(atten_tmp, dim=0)  # average across heads
        attns = []
        for r in range_:
            # att = torch.zeros(atten_tmp.shape[0], input_length)
            att = torch.zeros(input_length)
            for i in range(r[0], r[1] + 1):
                if i == 0:
                    continue
                v = atten_tmp[i - 1][: r[0]]  # 上一位的
                v = v / v.sum()
                t = torch.zeros(input_length)
                t[: r[0]] = v
                att += t
            att /= r[1] - r[0] + 1
            # merge token for att
            att = torch.tensor([att[rr[0] : rr[1] + 1].sum() for rr in range_])
            attns.append(att)

        # print(f"attentions: {attns}")
        # 计算每个超过阈值的 token 在前文的 attentions
        forward_attns = torch.zeros(len(tokens))
        hit_cnt = 0
        for i in range(len(curr_hit)):
            if curr_hit[i] == 1:
                forward_attns += attns[curr_st + i]
                hit_cnt += 1
        forward_attns /= hit_cnt
        forward_attns = forward_attns.tolist()

        # 分析词性，保留实词对应的 attns
        doc = nlp(all_text)
        real_words = set(
            token.text
            for token in doc
            if token.pos_ in ["NOUN", "ADJ", "VERB", "PROPN", "NUM"]
        )

        def match(token):
            for word in real_words:
                if word in token:  # note the direction of inclusivity
                    return True
            return False

        real_pairs = []
        for i in range(len(tokens)):
            tok, att = tokens[i], forward_attns[i]
            if i >= curr_st and curr_hit[i - curr_st]:
                continue
            if match(tok):
                real_pairs.append((att, tok, i))

        # logger.info(f"Real pairs: {real_pairs}")

        if "retrieve_keep_top_k" in self.__dict__:
            top_k = min(self.retrieve_keep_top_k, len(real_pairs))
        elif "retrieve_keep_ratio" in self.__dict__:
            top_k = int(len(real_pairs) * self.retrieve_keep_ratio)

        real_pairs = sorted(real_pairs, key=lambda x: x[0], reverse=True)
        real_pairs = real_pairs[:top_k]
        real_pairs = sorted(real_pairs, key=lambda x: x[2])
        return " ".join([x[1] for x in real_pairs])

    def inference(self, question, demo, case):
        # assert self.query_formulation == "direct"
        # print(question)
        # print("#" * 20)
        text = ""
        while True:
            old_len = len(text)
            prompt = "".join([d["case"] + "\n" for d in demo])
            tmp_li = [
                case,
                f"User Query: {question}",
                f"Answer generated so far: {text}",
            ]
            prompt += (
                "\n".join(s for s in tmp_li if len(s) > 0) + "\nRefine the answer here:"
            )
            # print('#### Prompt:', prompt)  # prompt = demos + case (???) + text
            # prompt += case + " " + text
            new_text, tokens, attns, logprobs, entropies = self.generator.generate_attn(
                prompt,  # does not include question
                self.generate_max_length,
                # self.attention_solver,
                use_entropy=self.method == "dragin",
                use_logprob=self.method == "attn_prob",
            )
            weight = entropies if self.method == "dragin" else [-v for v in logprobs]

            if self.use_counter == True:
                self.counter.add_generate(new_text, self.generator.tokenizer)
            hallucination, ptext, curr_tokens, curr_hit = self.modifier(
                new_text, tokens, attns, weight
            )

            if not hallucination:
                text = text.strip() + " " + new_text.strip()
            else:
                forward_all = [question, text, ptext]
                forward_all = " ".join(s for s in forward_all if len(s) > 0)

                def fetch_last_n_tokens(text, num, tokenizer=self.generator.tokenizer):
                    tokens = tokenizer.tokenize(text)
                    if num >= len(tokens):
                        return text
                    last_n_tokens = tokens[-num:]
                    last_n_sentence = " ".join(last_n_tokens)
                    return last_n_sentence

                if self.query_formulation == "current":
                    retrieve_question = " ".join(curr_tokens)

                elif self.query_formulation == "current_wo_wrong":
                    retrieve_question = " ".join(
                        list(
                            curr_tokens[i] if curr_hit[i] == 0 else ""
                            for i in range(len(curr_tokens))
                        )
                    )

                elif self.query_formulation == "forward_all":
                    retrieve_question = forward_all

                elif self.query_formulation == "last_sentence":
                    retrieve_question = self.get_last_sentence(forward_all)

                elif self.query_formulation == "last_n_tokens":
                    assert "retrieve_keep_top_k" in self.__dict__
                    retrieve_question = fetch_last_n_tokens(
                        forward_all, self.retrieve_keep_top_k
                    )

                elif self.query_formulation == "real_words":
                    retrieve_question = self.keep_real_words(
                        prev_text=question
                        + " "
                        + text
                        + " "
                        + ptext,  # does not include context
                        curr_tokens=curr_tokens,
                        curr_hit=curr_hit,
                    )
                else:
                    raise NotImplementedError(f"{self.query_formulation} not supported for QFS...")
                print(f"Next retrieve question: {retrieve_question}")

                docs = self.retrieve(retrieve_question, topk=self.retrieve_topk)
                prompt = "".join([d["case"] + "\n" for d in demo])
                prompt += "Context:\n"
                for i, doc in enumerate(docs):
                    prompt += f"[{i+1}] {doc}\n"
                prompt += "Answer the user query below ONLY using the context above.\n"
                prompt += f"User Query: {question}\n"
                tmp_li = [case, f"Answer generated so far: {text + ' ' + ptext}"]
                prompt += (
                    "\n".join(s for s in tmp_li if len(s) > 0)
                    + "\nRefine the answer here:"
                )
                # print('##### Prompt:', prompt)
                # prompt += case + " " + text + " " + ptext.strip()
                new_text, _, _, _ = self.generator.generate(
                    prompt, self.generate_max_length
                )
                if self.use_counter == True:
                    self.counter.add_generate(new_text, self.generator.tokenizer)
                    self.counter.hallucinated += 1
                # new_text = self.get_top_sentence(new_text)
                tmp_li = [text.strip(), ptext.strip(), new_text.strip()]
                text = " ".join(s for s in tmp_li if len(s) > 0)
                # text = text.strip() + " " + ptext.strip() + " " + new_text.strip()

                # print("### retrieve_question ###")
                # print(retrieve_question)
                # context = "### Context: ###\n"
                # for i, doc in enumerate(docs):
                #     context += f"[{i+1}] {doc}\n"
                # print(context)
                print("Answer:", text)

            # 判断 token 的个数要少于 generate_max_length
            tokens_count = len(self.generator.tokenizer.encode(text))
            if (
                tokens_count > self.generate_max_length
                or len(text) <= old_len
                or "the answer is" in text
            ):
                break
        # print("#" * 20)
        return text
