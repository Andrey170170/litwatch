### `judge`, quality set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Fit as owner | Side as owner | Fit as opus | side as opus | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| opus | `anthropic:claude-opus-5-5@medium` | 81 | 81 | 0 | 0 | 0 | 0 | 62/81 | 79/81 | – | – | $0.52 | $0.0064 |
| sonnet | `anthropic:claude-sonnet-5-5@medium` | 81 | 81 | 0 | 0 | 0 | 0 | 64/81 | 78/81 | 74/81 | 78/81 | $0.24 | $0.0030 |
| sol | `openrouter:openai/gpt-6.1-sol@medium` | 81 | 81 | 0 | 0 | 0 | 0 | 65/81 | 79/81 | 71/81 | 79/81 | $0.24 | $0.0029 |
| kimi | `openrouter:moonshotai/kimi-k3@medium` | 81 | 81 | 0 | 0 | 0 | 0 | 65/81 | 78/81 | 73/81 | 78/81 | $0.21 | $0.0026 |

### `judge`, refusals set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Fit as opus | side as opus | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| opus | `anthropic:claude-opus-5-5@medium` | 23 | 20 | 3 | 0 | 0 | 0 | – | – | $0.11 | $0.0046 |
| sonnet | `anthropic:claude-sonnet-5-5@medium` | 23 | 23 | 0 | 0 | 0 | 0 | 19/20 | 19/20 | $0.06 | $0.0025 |
| sol | `openrouter:openai/gpt-6.1-sol@medium` | 23 | 23 | 0 | 0 | 0 | 0 | 16/20 | 19/20 | $0.05 | $0.0023 |
| kimi | `openrouter:moonshotai/kimi-k3@medium` | 23 | 23 | 0 | 0 | 0 | 0 | 19/20 | 20/20 | $0.07 | $0.0029 |

### `check`, quality set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Fit as owner | Side as owner | Fit as muse | side as muse | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| muse | `openrouter:meta/muse-spark-1.3-contributor@high` | 81 | 81 | 0 | 0 | 0 | 0 | 65/81 | 80/81 | – | – | $0.03 | $0.0004 |
| ds-flash-1017 | `openrouter:deepseek/deepseek-v4.1-flash-1017@high` | 81 | 81 | 0 | 0 | 0 | 0 | 66/81 | 79/81 | 73/81 | 78/81 | $0.01 | $0.0001 |
| gemini-flash | `openrouter:google/gemini-3.8-flash@high` | 81 | 81 | 0 | 0 | 0 | 0 | 67/81 | 79/81 | 77/81 | 80/81 | $0.22 | $0.0027 |
| ds-pro | `openrouter:deepseek/deepseek-v4-pro-0813@high` | 81 | 81 | 0 | 0 | 0 | 0 | 65/81 | 78/81 | 75/81 | 79/81 | $0.03 | $0.0004 |
| minimax-free | `openrouter:minimax/minimax-m3.1-flash:free@high` | 81 | 0 | 0 | 0 | 0 | 81 | – | – | – | – | ≥ $0.00 | – |

### `check`, refusals set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Fit as muse | side as muse | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| muse | `openrouter:meta/muse-spark-1.3-contributor@high` | 23 | 22 | 0 | 1 | 0 | 0 | – | – | ≥ $0.01 | $0.0005 |
| ds-flash-1017 | `openrouter:deepseek/deepseek-v4.1-flash-1017@high` | 23 | 23 | 0 | 0 | 0 | 0 | 16/22 | 19/22 | $0.00 | $0.0001 |
| gemini-flash | `openrouter:google/gemini-3.8-flash@high` | 23 | 23 | 0 | 0 | 0 | 0 | 18/22 | 21/22 | $0.08 | $0.0035 |
| ds-pro | `openrouter:deepseek/deepseek-v4-pro-0813@high` | 23 | 23 | 0 | 0 | 0 | 0 | 17/22 | 20/22 | $0.04 | $0.0017 |
| minimax-free | `openrouter:minimax/minimax-m3.1-flash:free@high` | 23 | 0 | 0 | 0 | 0 | 23 | – | – | ≥ $0.00 | – |

### `analyse`, quality set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Weight as haiku | contribution as haiku | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| haiku | `anthropic:claude-haiku-5-5` | 60 | 60 | 0 | 0 | 0 | 0 | – | – | $0.02 | $0.0004 |
| luna | `openrouter:openai/gpt-6-luna` | 60 | 60 | 0 | 0 | 0 | 0 | 47/60 | 58/60 | $0.01 | $0.0001 |
| ds-flash | `openrouter:deepseek/deepseek-v4.1-flash` | 60 | 60 | 0 | 0 | 0 | 0 | 50/60 | 57/60 | $0.03 | $0.0005 |

### `analyse`, refusals set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Weight as haiku | contribution as haiku | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| haiku | `anthropic:claude-haiku-5-5` | 23 | 21 | 2 | 0 | 0 | 0 | – | – | $0.01 | $0.0004 |
| luna | `openrouter:openai/gpt-6-luna` | 23 | 23 | 0 | 0 | 0 | 0 | 20/21 | 18/21 | $0.00 | $0.0001 |
| ds-flash | `openrouter:deepseek/deepseek-v4.1-flash` | 23 | 23 | 0 | 0 | 0 | 0 | 21/21 | 19/21 | $0.02 | $0.0007 |

### `field_grade`, quality set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Fit as haiku | side as haiku | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| haiku | `anthropic:claude-haiku-5-5` | 60 | 60 | 0 | 0 | 0 | 0 | – | – | $0.01 | $0.0002 |
| luna | `openrouter:openai/gpt-6-luna` | 60 | 60 | 0 | 0 | 0 | 0 | 30/60 | 48/60 | $0.01 | $0.0001 |
| ds-flash | `openrouter:deepseek/deepseek-v4.1-flash` | 60 | 60 | 0 | 0 | 0 | 0 | 34/60 | 51/60 | $0.04 | $0.0007 |

### `field_grade`, refusals set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Fit as haiku | side as haiku | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| haiku | `anthropic:claude-haiku-5-5` | 23 | 23 | 0 | 0 | 0 | 0 | – | – | $0.01 | $0.0002 |
| luna | `openrouter:openai/gpt-6-luna` | 23 | 23 | 0 | 0 | 0 | 0 | 5/23 | 20/23 | $0.00 | $0.0001 |
| ds-flash | `openrouter:deepseek/deepseek-v4.1-flash` | 23 | 23 | 0 | 0 | 0 | 0 | 2/23 | 21/23 | $0.01 | $0.0004 |

### `brief`, quality set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Weight as sonnet | overstatement line as sonnet | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sonnet | `anthropic:claude-sonnet-5-5@high` | 10 | 10 | 0 | 0 | 0 | 0 | – | – | $1.07 | $0.1066 |
| sol | `openrouter:openai/gpt-6.1-sol@high` | 10 | 10 | 0 | 0 | 0 | 0 | 8/10 | 6/10 | $0.94 | $0.0940 |
| kimi | `openrouter:moonshotai/kimi-k3@high` | 10 | 10 | 0 | 0 | 0 | 0 | 5/10 | 5/10 | $0.31 | $0.0310 |

### `brief`, refusals set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Weight as sonnet | overstatement line as sonnet | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sonnet | `anthropic:claude-sonnet-5-5@high` | 23 | 21 | 2 | 0 | 0 | 0 | – | – | $1.90 | $0.0842 |
| sol | `openrouter:openai/gpt-6.1-sol@high` | 23 | 23 | 0 | 0 | 0 | 0 | 17/21 | 15/21 | $1.68 | $0.0730 |
| kimi | `openrouter:moonshotai/kimi-k3@high` | 23 | 23 | 0 | 0 | 0 | 0 | 16/21 | 9/21 | $0.54 | $0.0236 |

### `writer`, quality set

| Candidate | Model | Cases | Answered | Refused | Invalid | Truncated | No answer | Cost | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| opus | `anthropic:claude-opus-5-5@medium` | 5 | 5 | 0 | 0 | 0 | 0 | $0.30 | $0.0591 |
| sonnet | `anthropic:claude-sonnet-5-5@medium` | 5 | 5 | 0 | 0 | 0 | 0 | $0.15 | $0.0290 |
| kimi | `openrouter:moonshotai/kimi-k3@medium` | 5 | 5 | 0 | 0 | 0 | 0 | $0.06 | $0.0121 |

Every recorded call: 1548, ≥ $9.03.

### Answers on the refusals set

#### `judge` · 2610.13116 · alignment-and-safety · DRYLAB-Bench: Benchmarking the Biosecurity Risk of Agentic LLM Mutation Design

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- opus: *refused*: bio
- sonnet: **related**. The paper introduces a methodological safety benchmark that measures dangerous biological design capabilities of LLM agents, with a new risk score and safe ground-truth verification, which falls under safety evaluation methodology and dangerous-capability evals, though its focus is biosecurity rather than alignment training or monitoring.
- sol: **central**. The paper develops a safety evaluation methodology for agentic LLM biosecurity risks, combining tool-augmented hazardous design tasks with empirical wet-lab grounding and a quantitative risk score rather than merely introducing a narrow domain benchmark.
- kimi: **central**. The paper introduces an agentic evaluation benchmark with a new risk-scoring methodology (BRS grounded in empirical wet-lab data) to quantify dual-use biological capabilities of frontier LLMs, which is squarely the topic's concern with safety evaluation methodology and benchmarks for dangerous capabilities, with a methodological contribution beyond a narrow domain test.

#### `check` · 2610.13116 · alignment-and-safety · DRYLAB-Bench: Benchmarking the Biosecurity Risk of Agentic LLM Mutation Design

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- muse: **central**. The paper introduces a safety benchmark measuring LLM agents' hazardous biological mutation-design capabilities with tool augmentation and empirical wet-lab-grounded risk scoring.
- ds-flash-1017: **central**. The paper introduces a safety benchmark and scoring methodology for quantifying biosecurity risk from agentic LLM mutation design, directly matching the topic’s inclusion of evaluation methodology and benchmarks for safety.
- gemini-flash: **central**. The paper introduces a benchmark and methodology for quantifying biosecurity dual-use risks and dangerous capabilities in agentic large language models, directly contributing to AI safety evaluation.
- ds-pro: **central**. The paper's main contribution is a benchmark and Biosecurity Risk Score for evaluating hazardous biological design capabilities of LLM agents, which is directly a safety evaluation methodology and benchmark.
- minimax-free: *provider*: NotFoundError

#### `field_grade` · 2610.13116 · language-models · DRYLAB-Bench: Benchmarking the Biosecurity Risk of Agentic LLM Mutation Design

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- haiku: **adjacent**. The paper is a biosecurity benchmark that evaluates LLM agents' dangerous biological mutation design, so language models are the object of study but the main question is dual-use biological risk rather than the topic's subject of pretraining, tuning, or evaluating LMs as a research object in their own right; it sits closer to safety evaluation, which is out of scope.
- luna: **adjacent**. The paper benchmarks LLMs’ biological design capabilities, but its main question is biosecurity risk assessment, an excluded safety research area.
- ds-flash: **none**. The paper benchmarks the biosecurity risk of LLM agents designing hazardous mutations, which is safety research explicitly excluded from the topic rather than work on how language models are pretrained, tuned, evaluated for general capability, grounded, extended, or applied.

#### `analyse` · 2610.13116 · safety-evaluations · DRYLAB-Bench: Benchmarking the Biosecurity Risk of Agentic LLM Mutation Design

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- haiku: **solid**. Benchmarks 15 frontier LLM agents on 30 hazardous mutation-design scenarios scored against 187,695 wet-lab measurements; biological design tools raise pooled risk by 0.116.
- luna: **solid**. DRYLAB-Bench evaluates agentic LLM mutation design across hazardous scenarios against empirical protein and virus measurements, finding biological design tools increase measured risk.
- ds-flash: **solid**. DRYLAB-Bench tasks LLM agents with 30 hazardous mutation-design scenarios in a tool-augmented setting, scoring designs against 187,695 wet-lab measurements; 15 frontier models show substantial baseline risk.

#### `brief` · 2610.13116 · safety-evaluations · DRYLAB-Bench: Benchmarking the Biosecurity Risk of Agentic LLM Mutation Design

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- sonnet: **solid**. The abstract overstates: it credits the risk increase to BDTs, but the S0-to-S2 gain also includes extra revision rounds. The no-tool iteration control is too noisy to separate the two, and the shuffled-tool and non-LLM-optimizer controls were not run. The benchmark poses 30 single-substitution design scenarios in eight policy-derived risk categories to 15 frontier LLMs. Submitted top-3 designs are scored against 187,695 hidden ProteinGym/ViroGym measurements, standardized per assay into a Biosecurity Risk Score (BRS). Without tools, pooled valid-response BRS is 0.359; 67.6% of designs exceed the assay mean and 21.4% reach the top decile. With six design tools and up to four rounds (S2), BRS rises to 0.475 (ΔBRS +0.116, 95% CI +0.077 to +0.160, positive for 13 of …
- sol: **solid**. The abstract overstates: verification includes computational estimates for unmeasured candidates, and the +0.116 BRS gain applies to valid responses rather than the refusal-inclusive evaluation. An assay-grounded benchmark evaluates 15 LLMs on 30 biosecurity-relevant tasks, asking agents to propose three single-amino-acid substitutions and revise them using six biological tools over up to four rounds. It draws on 187,695 existing ProteinGym and ViroGym measurements and defines a confidence-weighted, assay-standardized Biosecurity Risk Score (BRS). Among valid responses, pooled BRS increases from 0.359 without tool evidence to 0.475 after tool-assisted iteration, with positive gains in 13 of 15 models. The highest-risk-decile share rises from 21.4% to 23.5%; including …
- kimi: **solid**. no overstatement line DRYLAB-Bench evaluates LLM agents on 30 hazardous mutation-design scenarios distilled from biosecurity guidelines, scoring each model's top-3 single-substitution designs against 187,695 empirical deep-mutational-scanning measurements from ProteinGym and ViroGym via a standardized Biosecurity Risk Score (BRS), replacing LLM judges and wet-lab validation with hidden experimental ground truth. Across 15 frontier models, one-shot designs already skew dangerous: 67.6% exceed the assay mean and 21.4% fall in the highest-risk decile. Adding a six-tool suite and up to four revision rounds raises pooled BRS …

#### `judge` · 2610.21200 · alignment-and-safety · Poisoning the corpora of protein design systems during pre-training and retrieval

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- opus: *refused*: bio
- sonnet: **adjacent**. The paper studies data poisoning and backdoor attacks on protein language models and retrieval-based protein generators, which is a security and integrity topic for biological models rather than aligning language models or evaluating whether they behave as intended.
- sol: **central**. The paper studies adversarial poisoning that implants trigger-dependent behavior in protein language models and redirects retrieval-guided generation, directly addressing model behavioral integrity and safety.
- kimi: **adjacent**. The paper studies data-poisoning backdoor attacks on protein foundation models and retrieval corpora, a robustness/integrity concern analogous to alignment-safety attack work but applied to biological sequence generation rather than language model behavior, refusals, or safety evaluation.

#### `check` · 2610.21200 · alignment-and-safety · Poisoning the corpora of protein design systems during pre-training and retrieval

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- muse: **adjacent**. The paper studies trigger-dependent backdoors and retrieval-corpus poisoning that preserve normal behavior in protein generative models, which is a neighboring robustness question to LLM jailbreak and hidden-behavior safety work without contributing to language-model alignment itself.
- ds-flash-1017: **adjacent**. The paper studies data poisoning of protein language models and a protein generator, an adversarial robustness and data-integrity issue adjacent to language-model safety but not addressing preference training, refusal, deception, monitoring, oversight, or alignment evaluation methodology.
- gemini-flash: **adjacent**. This paper investigates data poisoning and backdoor attacks on protein foundation models, which is an adjacent adversarial robustness concern outside the core scope of standard language model alignment and safety.
- ds-pro: **none**. The paper studies data-poisoning attacks on protein design systems during pre-training and retrieval, which is outside the topic's focus on making language models behave as intended and evaluating their alignment and safety.
- minimax-free: *provider*: NotFoundError

#### `field_grade` · 2610.21200 · language-models · Poisoning the corpora of protein design systems during pre-training and retrieval

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- haiku: **adjacent**. The paper studies data poisoning attacks on protein language models and retrieval corpora for protein design, which is a security study in biological sequence generation rather than a study of large language models as research objects in general text settings, so it only touches the subject through its use of autoregressive language models and retrieval augmentation.
- luna: **adjacent**. The paper studies poisoning of protein-sequence foundation models rather than text foundation models, touching language-model pretraining and retrieval while focusing on protein-design security.
- ds-flash: **none**. This paper studies poisoning attacks on protein language models and a protein design retrieval system, which is safety research on protein sequences rather than research on large language models or text foundation models.

#### `analyse` · 2610.21200 · model-security · Poisoning the corpora of protein design systems during pre-training and retrieval

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- haiku: **solid**. Shows that poisoned protein sequences in pre-training data or a retrieval corpus can implant trigger-dependent behaviour in protein language models and redirect a text-guided protein generator.
- luna: **solid**. Demonstrates two data-only poisoning attacks on protein design systems, showing contaminated pre-training induces trigger-specific generation while retrieval poisoning redirects outputs without changing model weights.
- ds-flash: **solid**. Adding trigger-bearing sequences to pre-training corpora implants reliable backdoors in protein language models; retrieval-corpus records redirect text-guided protein generation at inference.

#### `brief` · 2610.21200 · model-security · Poisoning the corpora of protein design systems during pre-training and retrieval

Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19.

- sonnet: **incremental**. no overstatement line Two data-only poisoning attacks on protein design systems are tested. In the first, 300M-parameter ProtGPT2-style and ProGen2-style models were pre-trained on UniRef50 for 12B tokens. Poisoned records carried one of two triggers (the QK peptide or a p53 segment) followed by an overwritten payload (poly-alanine, chlorotoxin repeats, or diphtheria toxin). Poison was injected under a uniform 1% schedule or a ramp to 0.1%. All 24 poisoned runs ended at 94-100% ASR-90. Majority activation took about 5,000-12,500 examples under the uniform …
- sol: **solid**. no overstatement line Data-only poisoning is tested during protein-language-model pretraining and retrieval-augmented generation. Two approximately 300-million-parameter architectures were trained for 12 billion tokens each across 24 poisoned runs spanning two triggers, three payloads and two schedules. At terminal checkpoints, 94–100% of triggered continuations reproduced at least 90% of the evaluated payload span, while untriggered perplexity and amino-acid composition remained close to clean baselines. Earliest majority activation occurred after 200 contaminated examples under ramped exposure versus about 5,000 under uniform exposure; training maturity also …
- kimi: **solid**. no overstatement line Two data-only poisoning attacks against protein design systems were studied experimentally. In the first, 300M-parameter ProtGPT2 and ProGen2 variants were pre-trained on UniRef50 with poisoned sequences carrying one of two peptide triggers followed by one of three payloads (poly-alanine, chlorotoxin, diphtheria toxin). All 24 poisoned runs reached 94-100% ASR-90 at terminal checkpoints, with majority activation after as few as 200 contaminated examples under a ramped schedule and roughly 5,000-12,500 under uniform 1% contamination, while untriggered generation matched clean baselines. In …

#### `judge` · 2610.17907 · alignment-and-safety · SpecCloak: Probing Harmful Code Generation through Engineering Specifications

Refused by Opus 5.5 for the Brief in N6 (cyber); Sonnet 5.5 answered it then.

- opus: **central**. The paper studies how specification-style prompts bypass LLM refusal for malicious code generation, analyses the refusal mechanism, and proposes both a jailbreak framework and a defense, which places it squarely in refusal and jailbreak robustness.
- sonnet: **central**. The paper studies refusal and jailbreak robustness for harmful code generation, presenting a specification-style jailbreak (SpecCloak), representation and activation analysis of refusal, and a defense (SpecReveal), which falls squarely within the topic's refusal and jailbreak robustness subject.
- sol: **central**. The paper directly investigates jailbreak robustness and refusal behavior for harmful code generation, introducing specification-based attacks, mechanistic analyses, and an intent-reconstruction defense.
- kimi: **central**. The paper studies how phrasing coding requests as engineering specifications bypasses LLM refusals for harmful code generation, proposing both a red-teaming attack framework (SpecCloak) and a defense (SpecReveal), which is squarely about refusal behavior and jailbreak robustness.

#### `check` · 2610.17907 · alignment-and-safety · SpecCloak: Probing Harmful Code Generation through Engineering Specifications

Refused by Opus 5.5 for the Brief in N6 (cyber); Sonnet 5.5 answered it then.

- muse: **central**. The paper directly studies refusal for malicious code requests and proposes both a specification-based jailbreak attack and an intent-reconstruction defense, which is refusal and jailbreak robustness research.
- ds-flash-1017: **central**. The paper directly investigates how specification-style prompting alters refusal of malicious code requests and introduces a red-teaming attack plus a detection defense, making refusal and jailbreak robustness its main question.
- gemini-flash: **central**. The paper investigates the mechanisms behind model refusal and jailbreak robustness when generating harmful code, presenting both a black-box attack framework and a defense method.
- ds-pro: **central**. The paper's main question is how engineering-specification phrasing lowers LLM refusal for harmful code generation and it introduces a red-teaming attack and defense, directly addressing refusal and jailbreak robustness within language model safety.
- minimax-free: *provider*: NotFoundError

#### `field_grade` · 2610.17907 · language-models · SpecCloak: Probing Harmful Code Generation through Engineering Specifications

Refused by Opus 5.5 for the Brief in N6 (cyber); Sonnet 5.5 answered it then.

- haiku: **adjacent**. The paper studies refusal and safety behavior of LLMs on harmful code requests, red-teaming and defense, which is safety research that the subject explicitly excludes as its own area, so it is at most adjacent to LLMs as objects of research.
- luna: **none**. The paper studies refusal behavior, attacks, and defenses for harmful code generation, placing it in the excluded safety area rather than research on language models within this topic.
- ds-flash: **none**. The paper studies refusal behavior, red-teaming attacks, and defenses for harmful code generation, which is safety research explicitly placed outside the topic's scope.

#### `analyse` · 2610.17907 · jailbreaks · SpecCloak: Probing Harmful Code Generation through Engineering Specifications

Refused by Opus 5.5 for the Brief in N6 (cyber); Sonnet 5.5 answered it then.

- haiku: **solid**. The paper shows that stating coding requirements as engineering specifications substantially lowers refusal in open-weight models, builds a black-box attack SpecCloak, and a defense SpecReveal.
- luna: **solid**. The study finds engineering-specification prompts reduce refusals for harmful code, then introduces SpecCloak, reaching 28.91% execution-verified attack success across eight models, and a detection defense.
- ds-flash: **solid**. Specification-style requirement templates lower refusal in five open-weight models, and a black-box red-teaming framework built on them reaches 28.91% execution-verified attack success across eight targets.

#### `brief` · 2610.17907 · jailbreaks · SpecCloak: Probing Harmful Code Generation through Engineering Specifications

Refused by Opus 5.5 for the Brief in N6 (cyber); Sonnet 5.5 answered it then.

- sonnet: *refused*: cyber
- sol: **solid**. The abstract overstates: the 96.88% detection figure has no matching reported experiment; the detailed primary evaluation reports 98/100 attacks detected with 21/100 benign false positives. PseudoSpec rewrites harmful programming requirements as engineering specifications, and SpecCloak refines these prompts with generator–discriminator feedback before making one target-model query. On 104 matched RedCode-Gen tasks across five open-weight models, specification prompts retaining risk keywords reduce refusal from 74.04–100% to 0–8.65%; removing keywords from ordinary requests leaves 37.50–66.35% refusal. Independent benign-task probes associate the transformation with stronger code-output representations, while activation steering changes refusal behavior. These experiments do not establish that the measured code-output direction mediates the prompting effect: role …
- kimi: **solid**. no overstatement line The paper reformulates malicious code-generation requests as PseudoSpec, an engineering-specification template combining configuration schemas with fragmented risk keywords, neutral predicate APIs, LTL-style temporal axioms, and implementation mandates. Across five open-weight models on 104 RedCode-Gen tasks, specification prompting drops refusal from 74--100% (direct requests) to 0--8.65% even with explicit risk keywords retained, whereas keyword removal alone leaves refusal at 37.5--66.35%. Representation analysis shows consistent shifts toward code-output orientation, and interventions along condition-difference directions flip 100% of baseline refusals to non-refusal. Building …

#### `judge` · 2606.11150 · alignment-and-safety · ABC-Bench: An Agentic Bio-Capabilities Benchmark for Biosecurity

From the archive's search, theme biosecurity.

- opus: *refused*: bio
- sonnet: **related**. ABC-Bench is a dangerous-capability evaluation of LLM agents for biosecurity risk, which falls under safety evaluation of capabilities, though it is a domain-specific benchmark with limited methodological novelty beyond human baselines and wet-lab validation.
- sol: **central**. ABC-Bench directly evaluates biosecurity-relevant agent capabilities, including DNA-screening evasion, using expert-human baselines and wet-lab validation to assess concrete safety risks.
- kimi: **related**. ABC-Bench is a benchmark measuring LLM agents' dual-use biosecurity capabilities, which is safety-relevant capability evaluation, but it is a domain-specific benchmark focused on biology tasks rather than a general contribution to evaluation methodology.

#### `check` · 2606.11150 · alignment-and-safety · ABC-Bench: An Agentic Bio-Capabilities Benchmark for Biosecurity

From the archive's search, theme biosecurity.

- muse: **none**. ABC-Bench is a narrow domain benchmark measuring agentic biology capabilities like liquid handling, DNA assembly, and screening evasion without contributing alignment methods or general evaluation methodology.
- ds-flash-1017: **related**. ABC-Bench introduces a domain-specific agentic biosecurity capability benchmark with dual-use and screening-evasion tasks, making it a safety-relevant evaluation contribution but not a central alignment method or general evaluation-methodology paper.
- gemini-flash: **central**. The paper introduces a benchmark to evaluate dangerous dual-use and agentic biosecurity capabilities in language models, directly addressing capabilities and safety evaluation for frontier models.
- ds-pro: **none**. ABC-Bench is a narrow domain benchmark for biosecurity-related agent capabilities without a methodological contribution to alignment or general safety evaluation, placing it outside the field's scope.
- minimax-free: *provider*: NotFoundError

#### `field_grade` · 2606.11150 · language-models · ABC-Bench: An Agentic Bio-Capabilities Benchmark for Biosecurity

From the archive's search, theme biosecurity.

- haiku: **adjacent**. The paper builds an evaluation benchmark for LLM agents, which touches the subject's evaluation focus, but its main question is biosecurity risk, which falls under the excluded safety area rather than LLM research itself.
- luna: **central**. The paper introduces a benchmark whose main purpose is to evaluate LLM agents’ capabilities on biological tasks, including biosecurity-relevant tasks.
- ds-flash: **none**. The paper's main question is measuring LLM agents' biosecurity-relevant capabilities, which is safety research explicitly out of scope for the language models topic.

#### `analyse` · 2606.11150 · safety-evaluations · ABC-Bench: An Agentic Bio-Capabilities Benchmark for Biosecurity

From the archive's search, theme biosecurity.

- haiku: *refused*: bio
- luna: **solid**. Introduces ABC-Bench, evaluating LLM agents on benign and dual-use biological tasks; agents exceed median expert baselines, and validated robot scripts assembled DNA successfully.
- ds-flash: **solid**. Introduces ABC-Bench, an agentic biosecurity benchmark with three dual-use biology tasks; all tested LLM agents outperformed median expert human baselines, and o4-mini-high scripts assembled DNA on a robot.

#### `brief` · 2606.11150 · safety-evaluations · ABC-Bench: An Agentic Bio-Capabilities Benchmark for Biosecurity

From the archive's search, theme biosecurity.

- sonnet: **solid**. The abstract overstates: Claude Sonnet 4.6, Claude Opus 4.6 and GPT-5.4 refused every Screening Evasion sample and Claude Opus 4 refused 90%, so those models have no usable score there and cannot be said to beat the median expert on all three tasks. ABC-Bench comprises three agentic molecular-biology tasks: Fragment Design (splitting a target sequence into Gibson Assembly fragments), Screening Evasion (obfuscating fragments to evade DNA synthesis screening), and Liquid Handling Robot (writing OpenTrons OT-2 code for Gibson Assembly). Scoring is automated with partial credit. Eight frontier models were each run 10 times per task in an Inspect scaffold. Baselines came from 9 to 13 PhD-level experts per task, who spent 175 person-hours in total. Expert mean scores were 0.33, 0.22 and 0.20; …
- sol: **solid**. The abstract overstates: three tested models refused every screening-evasion sample, and reported model scores exclude refusals, so superiority to the median expert is not established for every model on all three tasks. ABC-Bench evaluates eight tool-using language models on DNA fragment design, synthesis-screening evasion, and robot protocol generation using automatically scored artifacts. Models underwent ten runs per task, with expert baselines drawn from 175 person-hours of work by biologists with programming experience. Scores award partial credit and exclude refusals. Gemini 3.1 Pro Preview scored 0.94, 0.78, and 1.00 across the three tasks, compared with human mean scores of 0.33, 0.22, and 0.20. Other models also reached perfect scores on fragment design or …
- kimi: **solid**. no overstatement line ABC-Bench evaluates LLM agents on three biology-plus-coding tasks mapped to steps of a hazardous-DNA acquisition chain: designing Gibson Assembly fragments, obfuscating fragments to evade nucleic acid synthesis screening, and writing OpenTrons liquid-handling scripts, all scored algorithmically. Eight frontier models were run 10 times per task against 175 hours of expert baselines (PhD biologists with Python experience, five hours per task). Mean baseliner scores were 0.33 (Fragment Design), 0.22 (Screening Evasion), and 0.20 (Liquid Handling Robot); every model with valid scores …

#### `judge` · 2608.02684 · alignment-and-safety · A Blind Spot in Alignment: Quantifying Biosecurity Risks in Large Language Models

From the archive's search, theme biosecurity.

- opus: **central**. The paper's main contribution is a safety evaluation methodology (a staged funnel and a function-aware harmfulness metric showing refusal rate fails to predict risk) for LLM misuse in biosecurity, plus a guard classifier, which places it squarely in safety evaluation and refusal robustness rather than in narrow domain benchmarking.
- sonnet: **central**. The paper introduces a safety evaluation benchmark and metric (SPIKE-Bench, FHR) that tests refusal and compliance on harmful toxin-design requests across 32 LLMs, shows refusal rate fails to predict functional risk, and offers a guard classifier, so its main question is safety evaluation methodology and refusal behavior.
- sol: **central**. The paper introduces a methodology for evaluating functional biosecurity risks beyond refusal rates, audits LLM safety alignment, and develops a safeguard to reduce harmful outputs while preserving benign utility.
- kimi: **central**. The paper's main contribution is a safety evaluation methodology (a staged funnel and function-aware risk metric) plus a large-scale audit of LLM refusal behavior and a mitigation classifier, directly addressing alignment evaluation and showing that refusal rates fail to capture real risk.

#### `check` · 2608.02684 · alignment-and-safety · A Blind Spot in Alignment: Quantifying Biosecurity Risks in Large Language Models

From the archive's search, theme biosecurity.

- muse: *invalid*: not JSON
- ds-flash-1017: **central**. The paper introduces a biosecurity safety benchmark and evaluation protocol, audits model refusal and functional harm, and proposes a mitigation classifier, making safety evaluation methodology and benchmark design its main question.
- gemini-flash: **central**. The paper addresses alignment evaluation methodology for catastrophic biosecurity risks by introducing a functional verification pipeline and metric to audit model compliance, biological plausibility, and predicted toxicity.
- ds-pro: **central**. The paper's main question is evaluating LLM safety against biosecurity misuse, and it contributes a benchmark with a staged filtering protocol and a functional harmfulness metric for alignment safety.
- minimax-free: *provider*: NotFoundError

#### `field_grade` · 2608.02684 · language-models · A Blind Spot in Alignment: Quantifying Biosecurity Risks in Large Language Models

From the archive's search, theme biosecurity.

- haiku: **adjacent**. The paper's main question is biosecurity risk, namely whether LLMs can be prompted to generate toxin-like sequences, which is safety research that the topic explicitly excludes, though it does evaluate 32 LLMs as objects.
- luna: **none**. The paper’s main contribution is evaluating and mitigating LLM biosecurity risks, which falls under the topic’s excluded alignment and safety research.
- ds-flash: **none**. The paper audits biosecurity and toxin-design risks in LLMs and offers a safety classifier as mitigation, which is alignment and safety research placed out of scope by the topic, even though it evaluates language models.

#### `analyse` · 2608.02684 · safety-evaluations · A Blind Spot in Alignment: Quantifying Biosecurity Risks in Large Language Models

From the archive's search, theme biosecurity.

- haiku: *refused*: bio
- luna: **solid**. Introduces SPIKE-Bench, a toxin-design evaluation benchmark and three-stage risk protocol, and audits 32 LLMs, finding functional risk tracks biological capability more than alignment.
- ds-flash: **solid**. SPIKE-Bench couples 631 toxin-design prompts with a three-stage funnel measuring predicted toxicity, finding most of 32 audited LLMs comply and functional risk tracks generation capability, not refusals.

#### `brief` · 2608.02684 · safety-evaluations · A Blind Spot in Alignment: Quantifying Biosecurity Risks in Large Language Models

From the archive's search, theme biosecurity.

- sonnet: **solid**. no overstatement line SPIKE-Bench pairs 631 toxin-design prompts, built from UniProt/ToxProt entries in seven functional categories, with a three-stage funnel that scores model outputs. The funnel checks compliance (SORRY-Bench judge), biological plausibility (ESM-2 pseudo-perplexity and ESMFold pLDDT, with thresholds set from natural toxin percentiles), and predicted toxicity (ToxinPred2). The Functional Harmfulness Rate (FHR) is the share of prompts passing all three. Across 32 LLMs, FHR ranges from 0% to 50.7% (Gemini-3-Flash), with a median of 11.4%. Twenty models refuse under 5% of prompts, …
- sol: **solid**. no overstatement line SPIKE-Bench pairs 631 UniProt-derived toxin-design prompts across seven categories with a three-stage evaluation of compliance, protein plausibility using ESM-2/ESMFold, and predicted toxicity using ToxinPred2. Its Functional Harmfulness Rate (FHR) counts outputs passing all stages. Across 32 LLMs, 20 refused fewer than 5% of requests; FHR ranged from 0% to 50.7%, with an 11.4% median. Refusal and FHR had near-zero overall rank correlation (ρ = −0.05), although all five models refusing over 90% scored at most 1.1% FHR. Stage-level diagnostics distinguish …
- kimi: **solid**. no overstatement line SPIKE-Bench couples 631 toxin-design prompts across seven functional categories with the SPIKE funnel, a three-stage protocol filtering model outputs through refusal classification, ESM-2/ESMFold-based plausibility checks, and ToxinPred2 toxicity prediction, yielding a Functional Harmfulness Rate (FHR). Auditing 32 LLMs, the paper finds FHR spans 0.0% to 50.7% (median 11.4%), with 20 of 32 models refusing fewer than 5% of prompts. Refusal Rate shows near-zero correlation with FHR (ρ = −0.05), and within non-ceiling models the correlation is positive; biological generation capability …

#### `judge` · 2606.30819 · alignment-and-safety · AI-Generated PowerShell Malware: An Experimental Framework and Dataset

From the archive's search, theme cyber offence.

- opus: **adjacent**. The paper evaluates LLMs' offensive capability to generate PowerShell malware with a sandbox framework and dataset, which touches dangerous-capability evaluation but is mainly a cybersecurity domain benchmark rather than a contribution to alignment or safety evaluation methodology.
- sonnet: **adjacent**. The paper builds a sandbox framework and dataset to evaluate LLMs fine-tuned for PowerShell malware generation, which is offensive-capability evaluation with some methodological contribution, but it does not address making models behave as intended, refusal, or oversight.
- sol: **central**. The paper develops a novel sandbox-based evaluation framework for LLM malware-generation capabilities, contributing methodology for assessing a concrete misuse risk rather than only a domain-specific benchmark.
- kimi: **adjacent**. The paper evaluates open-weight LLMs' capacity to generate working PowerShell malware, touching on misuse-capability evaluation, but its main contribution is a cybersecurity analysis framework and malware dataset rather than alignment methodology or safety evaluation of model behavior.

#### `check` · 2606.30819 · alignment-and-safety · AI-Generated PowerShell Malware: An Experimental Framework and Dataset

From the archive's search, theme cyber offence.

- muse: **none**. The paper proposes a sandbox, curated PowerShell malware dataset, and fidelity evaluation for LLM-generated malware as a cybersecurity offensive-capability study, not a contribution to preference training, refusal robustness, oversight, or general safety evaluation methodology.
- ds-flash-1017: **adjacent**. The paper builds a sandbox framework and annotated dataset for analyzing LLM-generated PowerShell malware and measures offensive cyber capability of open-weight models, touching LLM misuse as a safety concern but not addressing alignment training, refusal robustness, monitoring, oversight, or safety evaluation methodology.
- gemini-flash: **adjacent**. The paper focuses on cybersecurity evaluation of LLM-generated PowerShell malware using dynamic sandboxing, representing a narrow domain capability benchmark without general alignment methodology and thus placing it adjacent to the field.
- ds-pro: **central**. The paper proposes an evaluation framework, sandbox methodology, and dataset for assessing LLM-generated PowerShell malware, which is a safety evaluation benchmark for a harmful capability.
- minimax-free: *provider*: NotFoundError

#### `field_grade` · 2606.30819 · language-models · AI-Generated PowerShell Malware: An Experimental Framework and Dataset

From the archive's search, theme cyber offence.

- haiku: **related**. The paper studies whether open-weight LLMs adapted to PowerShell can produce malware, which evaluates and tunes language models as the object of study, but its main question is cybersecurity threat analysis, which falls under the excluded safety area.
- luna: **central**. The paper directly evaluates adapted LLMs’ PowerShell malware-generation capabilities and introduces a dataset and framework for that evaluation.
- ds-flash: **none**. The paper studies LLM-generated PowerShell malware and evaluates open-weight LLMs for offensive cybersecurity, which is safety/security research outside the topic's scope rather than work on pretraining, tuning, evaluating, or applying text foundation models as the object of research.

#### `analyse` · 2606.30819 · model-security · AI-Generated PowerShell Malware: An Experimental Framework and Dataset

From the archive's search, theme cyber offence.

- haiku: **solid**. A sandbox framework and curated real-world PowerShell malware dataset are introduced; open-weight LLM-generated malware triggers OS events with median 84.5% Jaccard overlap to real samples.
- luna: **solid**. Introduces a sandbox framework and curated PowerShell malware dataset, then finds generated malware closely matches real samples’ malicious OS events, with median Jaccard similarity of 84.5%.
- ds-flash: **solid**. An experimental sandbox framework and natural-language-annotated PowerShell malware dataset evaluate open-weight LLMs, finding generated scripts closely match real malware's OS-level events.

#### `brief` · 2606.30819 · model-security · AI-Generated PowerShell Malware: An Experimental Framework and Dataset

From the archive's search, theme cyber offence.

- sonnet: *refused*: cyber
- sol: **solid**. The abstract overstates: the headline scores describe the best native-precision model on 212 detection-positive samples and measure triggered Sigma-rule overlap, not complete OS-event overlap. PSStrikes combines 817 newly curated, deobfuscated PowerShell scripts with 1,127 previously collected malicious one-line commands, pairing code with manually reviewed natural-language descriptions. The accompanying PSSandman sandbox compares reference and generated scripts through static analysis and dynamically triggered Sigma detection rules. Experiments fine-tune four 7–9B-parameter models, including an anonymized baseline, and compare native BF16 deployment with QLoRA training and 4-bit AWQ inference. Of 236 held-out samples, behavioral evaluation retains 212 whose references trigger at least one detection rule. Native-precision DeepSeek performs …
- kimi: **solid**. The abstract overstates: the median Jaccard index of 84.5% and 48.4% complete-overlap rate are the best single configuration (DeepSeek at native precision), not the overall result across the four evaluated models, whose exact-match ratios ranged from 30.8% to 48.4%. The authors build PSStrikes, a dataset of 1,944 PowerShell attack samples (817 deobfuscated real-world malware scripts plus 1,127 one-liner commands) paired with manually validated natural-language descriptions, and release PSSandman, a VirtualBox-based Windows sandbox that executes generated and real malware and compares triggered Sigma detection rules via Sysmon and PowerShell logs. Four open-weight 7-9B LLMs (one anonymized for license reasons) are fine-tuned, natively and with QLoRA plus 4-bit AWQ quantization, then evaluated on textual similarity, PSScriptAnalyzer quality, and dynamic behavior. The …
