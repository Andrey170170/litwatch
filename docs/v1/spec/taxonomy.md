# The class tree (draft)

A first draft for the owner to edit, in this file during the spec review and in Settings later. Sources: paperflow-lite's
22 collections, the seven 0.14 selections, and what cs.CL, cs.LG and cs.AI announce. Every paper intake stores must
fit somewhere, so the tree covers the whole intake shallowly and the owner's interests deeply.

Rules the draft follows:

- **Names say what they mean on their own** ("Chain-of-thought faithfulness", not "CoT faithfulness"): decision models
  choose by the name more than by the description.
- At most three levels. A class has a description of one or two sentences here; the followed classes reuse their
  topic's subject. Descriptions say what is out as well as what is in, where a neighbour is close.
- **Topics** are marked: [Focus], [Area, daily], [Area, weekly], [Field, weekly]. Today's seven 0.14 selections map
  onto them (CoT faithfulness and CoT monitoring were Watch; interpretability and alignment-and-safety were Radar;
  agents, efficiency-and-architecture and reasoning-and-rl were Pulse).
- "Other" exists once, at the top, for papers that fit nothing (stat.ME and similar strays).

About 55 classes. The decision request asks one yes/no question per class plus the primary choice, well under the
200 questions a Luna Decisions request allows.

## 1. Language models

Research whose main subject is large language models or other text foundation models, not covered by a more specific
top-level class.

| Class | Description |
| --- | --- |
| Pretraining, data and scaling | Pretraining objectives and recipes, training data curation, tokenization, scaling laws. |
| Instruction tuning and preference training | Supervised fine-tuning, RLHF, DPO and other preference optimization for general helpfulness. Reasoning-specific RL is under Reasoning; safety-motivated alignment is under Alignment and safety. |
| **Reasoning and RL post-training** [Field, weekly] | Training and using language models to reason: RL from verifiable rewards, GRPO-style post-training, reasoning models and thinking budgets, test-time scaling and search, self-verification, distillation of reasoning. |
| › Chain-of-thought prompting and test-time compute | Prompting methods, sampling and search at inference, thinking budgets. |
| › Reinforcement learning for reasoning | RL from verifiable rewards, reward models for reasoning, policy optimization methods. |
| › Analyses of reasoning models | What reasoning training changes, how reasoning traces behave, length and overthinking. |
| **Agents and tool use** [Field, weekly] | Language models acting over multiple steps: agent architectures, tool and function calling, planning and memory for agents, agent training. Papers using an off-the-shelf agent only as a baseline are out. |
| › Coding and software engineering agents | Repository-level coding, SWE-bench-style tasks, debugging agents. |
| › Web, GUI and computer-use agents | Browser and desktop control, screen understanding. |
| › Multi-agent systems | Several language-model agents interacting, cooperating or debating. |
| › Agent benchmarks and evaluation | Environments, benchmarks and metrics for agents. |
| **Efficiency and architecture** [Field, weekly] | How language models are built and run: architectures beyond the standard transformer, mixture of experts, long context, KV cache, quantization, pruning, distillation, speculative decoding, training and inference systems. Hardware kernels with no modelling change are out. |
| › Model architectures | Transformer variants, state-space and recurrent models, mixture of experts, looped and recursive models. |
| › Long context and memory | Context extension, memory mechanisms, context compression. |
| › Inference efficiency | Quantization, pruning, KV-cache methods, speculative decoding, serving. |
| › Training efficiency and systems | Distributed training, parallelism, optimizers at scale. |
| Retrieval-augmented generation | Retrieval, knowledge bases, grounding and citation for language models. |
| Hallucination, calibration and uncertainty | Factuality, truthfulness, confidence estimation, self-consistency. |
| Language model evaluation and benchmarks | General capability benchmarks, evaluation methodology, LLM-as-judge. Safety evaluations are under Alignment and safety; agent benchmarks under Agents. |
| Multimodal language models | Vision-language and audio-language models, multimodal reasoning. |
| Multilingual and low-resource languages | Cross-lingual transfer, languages other than English, low-resource settings. |
| Language tasks and applications | Machine translation, summarization, information extraction, dialogue, question answering, domain applications of language models (law, medicine, education). |

## 2. Interpretability [Area, daily]

Understanding the internal mechanisms, representations or circuits of neural networks, mostly language models:
probes, sparse autoencoders, attribution, activation patching, steering, feature and circuit discovery, and the methods
and evaluations for these tools. Work that only uses a probe or an ablation as an auxiliary measurement inside another
study is out.

| Class | Description |
| --- | --- |
| Features and representations | Probes, representation geometry, linear representations, superposition, neurons and directions. |
| Sparse autoencoders and dictionary learning | SAEs, transcoders, crosscoders, their training and evaluation. |
| Circuits and causal analysis | Circuit discovery, activation and path patching, causal tracing, attribution graphs. |
| Steering and model editing | Activation steering, representation engineering, knowledge and behaviour editing. |
| Automated interpretability | Model-written explanations, automated circuit discovery, interpretability agents and tooling. |
| Interpretability methodology and evaluation | Benchmarks, ground truth, faithfulness of explanations to the model, best practices. |
| Training dynamics and mechanisms of learning | How mechanisms form during training: grokking, phase changes, developmental interpretability. |
| **Chain-of-thought faithfulness** [Focus] | The topic's subject: whether a model's reasoning trace omits or contradicts a computation the model actually performed (BonaFide's definition). |

## 3. Alignment and safety [Area, weekly]

Making AI systems behave as intended and knowing whether they do. Narrow domain benchmarks without a methodological
contribution are out.

| Class | Description |
| --- | --- |
| Safety-motivated alignment training | Constitutional and value-based training, refusal training, character and honesty training. |
| Jailbreaks, red-teaming and robustness | Attacks on safety training, defences, adversarial prompts, red-teaming methods. |
| Prompt injection and model security | Prompt injection, data extraction, backdoors and poisoning, agent security. |
| Deception, sandbagging and scheming | Strategic deception, hidden objectives, sandbagging, evaluation awareness, model organisms of misalignment. |
| Reward hacking and specification gaming | Exploiting reward signals, specification gaming, emergent misalignment from training. |
| Scalable oversight and AI control | Debate, weak-to-strong supervision, control protocols, monitoring of actions. |
| Dangerous capabilities and safety evaluations | Evaluations of harmful capabilities, safety benchmarks, evaluation methodology for safety. |
| **Chain-of-thought monitoring** [Focus] | The topic's subject: reading a model's reasoning trace for oversight, monitorability, and evasion of monitors. |

## 4. Machine learning beyond language models

Shallow on purpose: these classes are not followed, so they only give a paper a primary class and Weight its colour.

| Class | Description |
| --- | --- |
| Learning theory and optimization | Generalization theory, optimization methods and analysis, not specific to language models. |
| Reinforcement learning | RL algorithms and theory outside language-model post-training. |
| Generative models for images, video and audio | Diffusion, flows, GANs, autoregressive media generation. |
| Computer vision | Recognition, detection, segmentation, 3D vision, video understanding. |
| Graph and geometric learning | Graph neural networks, geometric and equivariant learning. |
| Time series and tabular data | Forecasting, anomaly detection, tabular models. |
| Probabilistic and causal inference | Bayesian methods, uncertainty, causal discovery and inference. |
| Federated and privacy-preserving learning | Federated learning, differential privacy, unlearning. |
| Robotics and embodied AI | Robot learning, embodied agents, perception for control, autonomous driving. |
| AI for science and health | Applications to physics, chemistry, biology, medicine and other sciences. |
| Classical AI, planning and logic | Search, planning, constraint solving, knowledge representation, symbolic reasoning. |

## 5. AI and society

| Class | Description |
| --- | --- |
| Fairness, bias and social impact | Bias in models and data, fairness methods, social effects of AI. |
| AI policy, governance and economics | Regulation, governance, labour and economic effects, forecasting AI progress. |
| Human-AI interaction | User studies, AI assistance, education with AI, human factors. |

## 6. Other

Papers in the configured categories that fit none of the above.

## Open points for the owner

- Chain-of-thought faithfulness sits under Interpretability and Chain-of-thought monitoring under Alignment and safety,
  as now. A paper on both gets both classes.
- Alignment and safety is an Area with a weekly digest today; Interpretability is the only daily Area.
- Whether "Language models" itself should be followed as a Field (a weekly overview of everything LLM), with Agents,
  Efficiency and Reasoning as its followed children.
