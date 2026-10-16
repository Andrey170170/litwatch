**Verdict: keep the appendix. Eight of the 40 papers lose at least one material item supported only by the appendix in the supplied text.** The approved zero-loss rule therefore fails.

I compared every A/B pair, including Weight and Overstatement, against its supplied full text. No model or network calls were made. Findings concern these particular outputs; one A/B run cannot establish how reliably appendix removal causes a particular omission.

In the table, **body** means the item is available before the appendix, even if repeated later; **appendix only** means I found its substance only after the appendix boundary; **not found** means the supplied text does not establish it. Materiality asks whether the omitted information changes a researcher's understanding of evidence, reliability, scope, or use. Losing a precise number while retaining its substantive qualification is generally not material. Equivalent percentages, rounding, intervals written with different punctuation, and limitations retained in B's Weight/Overstatement are not losses.

Evidence locations are line numbers in `/tmp/p1-texts/<paper id>.latex.txt`, or named sections where a scope judgment requires reading the experimental design. All 40 inputs are LaTeX. I also recognized `\beginappendix` (13886), `\appendices` (15883), `appendixpart` (18407), and “Supplementary Material” (21200), rather than assuming only the literal `\appendix` marks an appendix. An assertion that a test was not conducted is sometimes an inference from the described study, identified below.

| Paper id | Item in A missing or weakened in B | Where found | Material? |
|---|---|---|---|
| 2610.17365 | 95% CI 16.1–29.3 for 22% deviation | body, L131 | No — B retains rate and n=150; uncertainty detail only. |
| 2610.21456 | Explicit +5.45 BLEU matched-budget gain | body, L778 | No — B retains 31.82 versus 26.37. |
| 2610.21456 | No standard circuit-discovery baseline comparison | body, Baselines/experiments, L422 onward; design inference | Yes — comparative support for the discovery method is limited; this is available without the appendix. |
| 2610.21456 | Explicit lack of a causal test of the English-pivot claim | body, MLP cosine/pivot analysis | No — B identifies the cosine evidence and “loose probes”; the evidential limitation survives. |
| 2610.15957 | Structural errors remain below 3% | body, L202, L231 | No — useful quality detail, but B retains the more consequential FMD deterioration. |
| 2610.19452 | J-Lens uses response-position features, unlike pre-generation detectors | body, L258 | Yes — changes whether a detector can screen before a response and qualifies comparison fairness. |
| 2610.19452 | A's template-dependence overstatement objection | body, L376; single-format design | No — B still explicitly says one template; it chooses another supported overstatement objection. |
| 2610.20852 | Causal ablation uses only 40 prompt/image examples per dataset | **appendix only**, L444 | **Yes — the sample size materially limits confidence in the causal explanation.** B retains one-model scope but loses this small-n qualification. |
| 2610.20852 | Large variation across prompt templates, not just question categories | **appendix only** for the observed template variance, L552–555; body L198/L285 describes the five-template protocol | **Yes — robustness depends on wording, not just truth/negation category.** The body gives the protocol but not this specific observed variance. |
| 2610.12989 | Random-ablation generation change is under 0.5 points | body, generated-narrativity experiment/plot | No — B retains “far more than random ablation.” |
| 2610.12989 | No extra OpenBookQA impairment, the non-narrative negative control | body, L113, L524, L563 | Yes — supports specificity rather than general damage; B omits it, but the body supplies it. |
| 2610.12989 | SAE ablation increases degenerate outputs; narrativity drop survives excluding them | **appendix only**, L897–952 | **Yes — establishes both a major confound and the check that the result survives it.** B reports the generation drop without either qualification. |
| 2610.15207 | LLaMA2 exit-15 AdvBench ASR 46% and full-depth 54%; exit-15 HarmBench 36% | body, ASR table/results | No — B retains the ranges and non-monotonicity; only particular endpoints disappear. |
| 2610.22127 | About 20% of parameters used per token | body, L71/L301 | No — efficiency magnitude omitted, but B states the active/total-parameter comparison. |
| 2610.22127 | Sparse probes cover 20 preference concepts | body, L200 | No — modest diagnostic-size detail. |
| 2610.22127 | Mixtral exception on latent sharing/disentanglement | body, RQ1 comparisons and L270 | No — B's Overstatement explicitly retains a Mixtral counterexample, though its Brief does not. |
| 2610.22127 | Explicit “expert attribution is correlational” in the Brief | body, L319–320 | No — B retains this in Weight and Overstatement. |
| 2610.18407 | No downstream or generation evaluation; one backbone family | body, study's experiments/scope; absence-of-test inference | Yes — reconstruction-perplexity gains do not establish generated-text or downstream utility. B's Weight still says no reference model, but not this evaluation limit. |
| 2610.18407 | Approximately 0.5% extra gain from three/four streams | body, stream-count results | No — B retains the more precise “at most 0.52%.” Not an actual loss. |
| 2610.18262 | Cisco Layered CSI detection 2.9%, SkillRisk detection zero | body, L501 | No — B retains that scanners flag at most 4.3%; individual scanner rates do not alter the takeaway. |
| 2610.18262 | Shortening confounds length with wording/organization | body, L510 | No — B says an LLM rewrote the skills and main rates may not extend to unmodified skills; substantially the same warning survives. |
| 2610.21261 | 384 sampled coalitions per prompt | body, L462 | No — sampling-budget detail. |
| 2610.21261 | Affine controls sit at numerical zero | body, L472 | No — useful estimator sanity check, but its absence does not change B's stated scope. |
| 2610.21261 | SAE-versus-rotation gap tiny for Mistral greater-than | appendix only, full rotation results; main body L496–499 only says gap size varies | No — B claims consistent direction, not a uniformly large gap. |
| 2610.21261 | Pairwise steering model is worse in some individual MMLU-Pro domains | **appendix only**, L1988–1993 (Economics, Engineering, Law, Math) | **Yes — the overall improvement does not guarantee a useful improvement within a researcher's domain.** Body L617 says “heterogeneous,” but does not state that some domains worsen. |
| 2610.20221 | Seven source benchmarks, including AIME 2024; specific resource list | body, abstract/source-resource section | No — B describes the benchmark types; omitted catalogue details do not alter validity. |
| 2610.20221 | Gemma-3-12B is the fidelity judge | appendix only, L723/L753 | No — identity detail by itself; B retains automatic judging. See B's incorrect extra claim below. |
| 2610.20221 | Harmful/benign label agreement with English is only 92.6%, despite 99.6% paired meaning-preservation judgments | **appendix only**, L723–753 | **Yes — exposes a safety-label fidelity problem hidden by the near-ceiling semantic score.** Judge noise also matters: English gold accuracy is only 88.7% (L725). |
| 2610.20221 | Llama unsafe-XSTest refusal 97.5%→69.5% in Turkish | appendix only, L786/L804 | No — B retains under-refusal outside English; this example's exact magnitude is not necessary to retain the limitation. |
| 2610.20221 | Qwen safe over-refusal 6.4%→13.0–23.7% across all 11 languages | appendix only, L790 | No — B retains over-refusal; the all-11 scope is also in the body, L551–553. |
| 2610.19709 | Gate-lag diagnosis is demonstrated for the unflagged setting; no direct test that late relevance causes the failure or explains why it arises | body, layerwise probe/patching results and discussion | Yes — distinguishes causal gate intervention from a causal explanation of the delay. B adds “correlational probes” but drops this precise qualification. |
| 2610.19768 | Progressive recipe learns length 32 in only one of three seeds | body, L129–131/L368 onward | No — B retains “mostly fails,” three seeds, and the full-gradient comparison. |
| 2610.19731 | Failed steering goal reach at most 0.027 | body, L840 | No — B retains that steering does nothing in these environments. |
| 2610.19731 | PushToMarker improves from 0.314 with cloning to 0.661 with DAgger | body, L870–873 | Yes — changes what a more complete repair achieves; B only reports the cloning result. |
| 2610.19731 | Attribute Maze random-trunk rewire reaches 0.840 | body, L887–888 | Yes — B adds 0.871 Attribute Maze recovery but omits this near-matching random baseline; its Coin random-baseline caveat does not cover Attribute Maze. |
| 2610.19731 | Agents are small CNNs | body, L600 | No — B retains small-RL-environment scope in Weight; architecture detail is secondary. |
| 2610.12842 | Orthogonal Swin content/style selectivity 7.2/2.6 | body, L476 | No — B retains invertible 16.1/5.3 versus permutation about 1. |
| 2610.12842 | Single TDR axes predict task variables but have no meaningful causal effect | **appendix only**, L1082–1102 | **Yes — a negative causal comparator materially qualifies what simpler, decodable axes establish versus the learned subspaces.** |
| 2610.12842 | Non-convex optimization | body, L641 | No — B retains hyperparameter sensitivity; the practical fragility remains. |
| 2610.22244 | Only 58 distinct user instructions underlie 1,220 labelled steps | body, L68/L84 | Yes — important effective dataset-size/independence qualification; B retains a clustering caveat but loses this count. |
| 2610.22244 | English paraphrase shifts general guards by at most 1.4 pp | body, L67 | No — B retains that paraphrase moves guards less than translation. |
| 2610.22244 | AgentDoG also reverses the abstract's restatement pattern | body, restatement analysis | No — B retains the pattern's failure for over-blockers and its restricted applicability. |
| 2610.22244 | Sonnet 5 and Gemini have no significant language shifts | body, L65 | No — B retains frontier guards' very small movement; the specific null-test statement disappears. |
| 2610.17301 | Four agent identities/sizes: Qwen3.8-27B, Muse-Glimmer-30B, Gemma-4-26B, gpt-oss-20b | body, model/evaluation setup | No — B retains four open-weight agents; model roster detail only. |
| 2610.17301 | Gemma's approver is a simulated principal applying scenario permissions | body, L386 | Yes — materially qualifies how realistic the prevention result is; B drops it, but it is not appendix-only. |
| 2610.17301 | Model-approver evidence consists of bounds on logged asks, not live episodes | body, Limitations L449; expanded appendix L1760 | Yes — a deployment-relevant restriction, already available without the appendix. |
| 2610.17301 | 25% escalations, confirmed on 87 fresh Qwen asks | body L386 has 25% and fresh-Qwen confirmation; **87** only appendix L1760 | No — B does not make a model-approver/escalation claim; the exact confirmatory n would qualify a claim it omits altogether. |
| 2610.17301 | gpt-oss episode violations only improve 0.78→0.60; modest Muse improvement | body, L374/L386 | Yes — B's “cuts ... in all four models” loses important effect-size heterogeneity. |
| 2610.17301 | Internal readout leads text before the plan exists | body, L428 | No — B retains parity once the plan is written in Weight and the hidden-reasoning advantage in the Brief. |
| 2610.19365 | Prior exposure consists of 1,000 harmful examples | body, L171/L287 | No — B retains the paired training paths; dose detail only. |
| 2610.19365 | Qwen2.5-32B judge identity | appendix only, L887 | No — B retains frozen, blinded LLM judging. |
| 2610.19365 | Human audit validates relevance, not harmful-goal/operational-role labels or the history contrast | body, L271–274; appendix L1051 onward | Yes — B's Weight calls the application “validated” without this restriction; the restriction is available in the body. |
| 2610.19365 | Early 4B behavioral gain +12.1 pp at 200 examples vanishes by 800 examples | **appendix only**, L547–551 | **Yes — limits persistence of the susceptibility difference.** B reports greater early susceptibility but omits convergence. Main text states an early difference and points to a trajectory figure, but does not supply this vanishing-at-800 result in the text. |
| 2610.19365 | Dimension contrasts reverse for 7B cyber/hate and 0.5B violence | **appendix only**, L628–630 with dimension mapping L1021–1028 | **Yes — prevents interpreting the positive aggregate as a uniform hazard-level effect.** Main text says “most” dimensions but does not identify these counterexamples. |
| 2610.19365 | Contrast is near zero at layer 0 | body, L331 | No — B retains the mid-depth peak; localization detail. |
| 2610.19365 | A says no reported test links accessibility to behavior | **not found as an established absence**: body L190 explicitly refers to a supplementary held-out association analysis | No — do not count A's stronger claim as an appendix-derived truth. The referenced analysis is not actually supplied under that label. |
| 2610.15634 | No missing number or limit | — | No — baseline repetitions, threshold, model rates, subset size, single-turn/synthetic/neutral-control limits all survive. A's Bradley–Terry weighting detail is a method detail; B retains LLM-judged weights. |
| 2610.18331 | Matched-size perceptual-alignment peaks roughly 0.2–0.26 | body, matched-n table L732–737 | No — B retains low alignment and matched-n robustness. |
| 2610.18331 | Explicit Gemma-9B mid-layer exception | body, cross-lingual-symmetry analysis | No — B retains model-dependent trajectories. |
| 2610.18331 | Limited to color | body, L945–946 | No — B clearly describes a one-domain color study, including in Weight. |
| 2610.16426 | 350 blocked base triplets, 700 base-plus-inverted instances | body, L712–714 | No — B retains construction and 1,400 total. |
| 2610.16426 | GLM FPRs 92.9%/97.1% | body, L805 | No — B retains at-least-80% FPRs and catastrophic over-blocking. |
| 2610.16426 | Label audit gives no sample size | not found: no audit denominator specified in supplied text | Yes — B calls the audit “small,” which does not convey that its size cannot be checked. Not an appendix-only loss. |
| 2610.16426 | No accuracy table for signal-augmented predictions | body, signals experiment/table inventory; absence-of-report inference | No — B retains the actual correction/misleading rates and their synthetic, label-informed origin. |
| 2610.15883 | Probe/routing tradeoff not established as fundamental | body, L1399–1408 | Yes — observed adaptive failures are not an impossibility result; B drops this explicit open question. |
| 2610.15883 | Defensive deployment ideas are not evaluated | body, Dual Use section and evaluation scope; inference | Yes — guards against treating proposed safeguards as tested deployments. No appendix-only evidence needed. |
| 2610.15883 | News-trained model fails to transfer to code | body, L1002 onward | No — B retains that the learned attack is domain-bound. |
| 2610.15883 | Thin replication at the largest scales | body, trained-checkpoint/evaluation roster; scope inference | No — secondary to B's already qualified adaptive analysis and case-study scope. |
| 2610.14190 | SAE placement is Qwen layer 15 / Gemma layer 19 | appendix only, L2317 | No — implementation detail in isolation. |
| 2610.14190 | Moving Gemma's SAE to layer 12 collapses GQA accuracy | **appendix only**, L2024–2043 | **Yes — directly limits the claim that inserting the SAE preserves task behavior.** Accuracy becomes 0.3315, about a 26-point drop. |
| 2610.14190 | Attention-based alignment supervision can be noisy/incomplete | **appendix only**, Limitations L1600 | **Yes — a central grounding signal is a proxy rather than validated correspondence.** B states the supervisor but omits its reliability qualification. |
| 2610.14190 | Candidate-pool overlap only 0.02–0.15 after image removal | appendix only, L2458–2478 | No — B retains drastic training-example ranking changes; exact support-overlap numbers add detail. |
| 2610.18122 | Emotional cue on better answer drops typical TDM accuracy 99%→42% | body, L418 | Yes — “sentiment is strongest” loses the magnitude of a severe failure. |
| 2610.18122 | Six TDMs repeat >99.7% of verdicts | body, L1095 | No — B retains greater consistency; precise rate only. |
| 2610.18122 | Emotional tone confounded with clarity/coherence | body, L1137–1138 | Yes — affects interpretation of the sentiment-bias result. |
| 2610.16371 | Correlations are measured on training projections | body, L159 | Yes — B reports strong correlations without this important generalization qualification. |
| 2610.16371 | Dir-A shifts all four quadrants, so acceptance bias is not excluded | body, L350–351 | Yes — B's ceiling-effect alternative does not explicitly retain the uniform-shift confound. |
| 2610.16371 | Cohen's d spans about 0.4–1.7 | body, steering effect-size table L284–298; expanded appendix L520 onward | No — B retains large quadrant effects and small accuracy improvement. |
| 2610.17907 | Local-judge detection can fall to 40/100 and 71/100 | body, defense table/results | No — B retains heavy judge dependence. |
| 2610.17907 | Spotlighting and Erase-and-Check are competitive | body, defense comparison L721 onward | Yes — materially qualifies whether the proposed defense adds value over existing approaches. |
| 2610.17907 | Defense tested on attacks generated against o4-mini, without adaptive defense-aware attacks | body, L716/L812; design inference | Yes — materially limits robustness generalization; B drops it. |
| 2610.13886 | GLM-5.2 easiest target, HPR 31.6% | body, L223 | No — B retains pooled rate and hardest target; another endpoint is not essential. |
| 2610.13886 | Refinement succeeds on 15.5% of previously failed paths | body, L295 | No — B describes the repair operator and retains another feedback ablation. |
| 2610.13886 | Alternative judge/OR rates 15–30 points higher | body, L362, with full table appendix L757–761 | No — B retains strong judge dependence. A's compressed range is not uniformly true for every single-judge contrast. |
| 2610.13886 | Two baselines were reimplemented | body, baseline/evaluation setup; expanded implementation appendix | No — B retains that baseline effort is unmatched, the more consequential caveat. |
| 2610.15778 | Sampling-sharpening check specifically at step 50 | body, protocol/results | No — B retains the sharpening result. |
| 2610.15778 | 3.5× onset contrast uses hacking threshold h≥0.5 | body, onset definitions/pre-specified-pair analysis | No — B retains the onset comparison; operational threshold detail only. |
| 2610.16084 | Lower endpoint 1.7× in the curvature-bound range | body, L230 | No — B retains up to 12×, median 4×, and the bound-versus-measurement qualification. |
| 2610.16084 | Bound is local to alpha=1 | body, L230 | Yes — limits formal extension to larger steering strengths; B omits this. |
| 2610.16084 | Random directions already anti-steer 60–86% in most conditions | body, L293 | Yes — qualifies how distinctive the sharp-direction result is. |
| 2610.16084 | Unsteered Qwen refusal baseline 78% | body, generation results L315/L320 | No — B retains CAA 36% versus cleaned 88–98%; useful but secondary comparator. |
| 2610.16084 | Per-position KL analysis attributes much distribution shift to the sharp part | body, L300–310 | No — supporting mechanism detail, not a lost limitation. |
| 2610.16084 | Negative-direction tests on two models: cleaned vector most effective jailbreak, sharp least | body, L329–332 | No — B retains negative-polarity tests and the noise-versus-signal interpretation; explicit ranking/model count omitted. |
| 2610.16084 | Fifty completions are greedy | body, generation protocol L310 | No — B retains 50 completions and narrow classifier-based evaluation; decoding detail only. |
| 2610.13116 | Refused units score zero | body, BRS/refusal scoring; expanded appendix metric definition | No — scoring-convention detail; B retains refusal rates. |
| 2610.13116 | No shuffled-tool or non-LLM-optimizer control | appendix only, L2315–2363 | No — B explicitly retains that tools and iteration are not separated, including the noisy no-tool control. The specific missing controls add remedies rather than a new limitation. |
| 2610.13116 | Low reasoning effort | body, L977 | Yes — materially limits extrapolation to models' stronger reasoning settings. |
| 2610.13116 | Fable 5 specifically refuses 96.7%; five models refuse none | body, L1189 | No — B retains the full 0–96.7% range and wording sensitivity. |
| 2610.18159 | Topics labelled by four models; difficulty assigned by three | body, L184–186 | No — annotation-detail loss; B retains nine topics/four difficulty levels. |
| 2610.18159 | Greedy decoding and substring-match scoring | body, inference/evaluation L232 onward | Yes — limits claims about semantic correctness and sampling robustness. |
| 2610.18159 | Four of the framing dimensions change accuracy by at most 0.04 | body, framing-effects analysis | No — B retains small effects and gives a tighter ≤0.02 bound for a subset. |
| 2610.22046 | Probe read site is layers 23–24 | body, L153 | No — architecture-specific implementation detail. |
| 2610.22046 | Detection margin is 0.05; 4B/8B type-classifier results straddle it | body, L146/L168 | Yes — determines which results meet the paper's success criterion; B retains numbers without that decision rule. |
| 2610.22046 | Miss-rate cutoff is 0.5 | body, L162 | No — useful operating-point detail but B retains miss rates and calibration context. |
| 2610.21200 | Ramp ends at 0.1% poisoning | body, L119/L314–316 | Yes — the contamination budget matters for feasibility; B gives the ramp's timing but not its rate. |
| 2610.21200 | ProGen2 passes 50% ASR at 200 examples | body, pretraining dose-response results | No — B retains activation beginning at 200–1,000 and late-poison scheduling. |
| 2610.21200 | Teacher-forced payload perplexity near unity | body, L140 | No — B retains 94–100% terminal ASR; corroborating measure. |
| 2610.21200 | Retrieval corpus has 712,248 records | body, L271/L356 | No — useful scale detail, but B retains insertion/capture numbers. |
| 2610.13928 | Random-oracle/min-entropy assumptions and security reduction | body, L643–719 | Yes — important distinction between conditional security proof and empirical attack resistance; B omits the theorem/assumptions. |
| 2610.13928 | Full-strength 24-round/128-bit model function-checked, not attacked | body, L843 and experiment configurations | Yes — qualifies what cryptographic-strength setting was empirically tested. B retains weakened experimental settings but omits the full-strength distinction. |
| 2610.20993 | Letter-permuted prior raises positional skew | body, L361 | No — counterfactual diagnostic detail; B retains prior-skew association and correction effects. |
| 2610.20993 | Reversal aggregation reduces flip rate by 0.54 log units | body, L483 | No — B retains which factor it lowers. |
| 2610.20993 | Placement range 16.7→9.6 / 8.2 pp | body, L480–483 | No — B retains both narrow the range and the rank-reversal comparison. |
| 2610.20993 | Mitigation comparison restricted to ≥3 options | body, L368/L500; appendix also L588 | Yes — restricts the comparison's scope. B instead introduces an incorrect exactness restriction; see below. |
| 2610.17457 | Prompted-model F1 range approximately 54–92 | body, L772–780 | No — B retains best-model performance. A's lower bound overlooks Llama's lower F1; not an appendix-loss argument. |
| 2610.17457 | Exact Opus versus guard Markdown/accessibility FPRs, 3.77–4.84% versus 5.77–7.40% | body, L772/L798 | No — B retains that Opus has lower FPR. |
| 2610.17457 | Detector inference cost about $0.18/1,000 pages | body, L798 | No — cost detail does not change validity. |
| 2610.17457 | Guard lowers adaptive ASR only from 93.55% to 84.18% on one agent | **appendix only**, L1622–1629 | **Yes — high static detection F1 leaves most adaptive attacks successful.** B drops the only direct guarded-adaptive outcome. |
| 2610.20840 | Near-range boundary <15 cm | body, L291/L309 | No — B retains separate near/far codes. |
| 2610.20840 | Probe cosine 0.04 versus split-half ceiling about 0.23 | body, L311 | Yes — ceiling supplies important reliability context for interpreting the small cosine. |
| 2610.20840 | Contact accuracy 91.3% | body, L318/L341 | No — B retains off-schedule contact decoding. |
| 2610.20840 | First gripper steering component explains about 70% variance | body, L391/L417 | No — B retains mostly one-dimensional edits and arm leakage. |
| 2610.17641 | Composition channel exceeds half the change in 17% of 99 configurations | body, L48/L637 | No — B retains median 4%; both characterize a mostly small, heterogeneous effect. |
| 2610.17641 | GCD reduces answer-rate shift 14-fold | body, L55 and matched comparison | No — B retains +0.0705→−0.0049, which conveys the magnitude. |
| 2610.17641 | Four-fold larger preference change; preserves PPL/MMLU; held-out-template evaluation | body, L55/L551–584 | Yes — B loses capability-cost and generalization evidence while retaining the unknown-rate gain. |
| 2610.18415 | FLAME helpfulness 37.7 versus 51.2; FactAlign retrain −2.64 pp | body, L443/L448 | No — B retains both qualitative reproduction failures. |
| 2610.18415 | KnowRL's apparent gain is refusal-driven, with 93% refusal | **body for the material caveat**, L420; **appendix only for exact 93%**, L775 | Yes for the lost refusal-driven explanation; no for 93% versus “most.” Not counted as an appendix-only material loss because the body already states the mechanism. |
| 2610.18415 | FactGRPO/FAITH lose about 18/25 pp on TruthfulQA | body, plotted data and common-results discussion L605 | Yes — a large regression is more informative than “no significant gain,” but it is body-available. |
| 2610.18415 | FactGRPO reward increases with supported but irrelevant claims; VeriScore ignores relevance | body, reward plot L583–599 and evaluation analysis | No — B retains rewards favoring superficial attributes and large irrelevant-claim rates; exact diagnostic relation is omitted. |
| 2610.18415 | Human audit: two authors, 300 claims | body, L627/L647 | Yes — constrains independent human validation; B omits the audit entirely. |
| 2610.18415 | FactGRPO runs at only 11% of original steps | body, L331; L423 says a tenth | No — B retains reduced budgets; exact fraction is secondary. |
| 2610.18415 | On-policy/offline comparison confounded by methods' data, rewards and supervised stages | body, L406/L476/L605; design inference | Yes — B's unqualified “perform alike” loses A's causal-attribution caveat. |

The eight counted papers are **2610.20852, 2610.12989, 2610.21261, 2610.20221, 2610.12842, 2610.19365, 2610.14190, and 2610.17457**. Each counts once, even when it loses several material items. This is **8/40 = 20% of this set**, not an estimated population-wide causal failure rate.

There are judgment calls at the margin: whether the TDR comparator, domain-specific steering regressions, or ablation n=40 deserves space in a short Brief. Excluding all three still leaves **five** papers with material appendix-only losses and leaves the rule's verdict unchanged. I did not count a precise appendix-only number as material when the same substantive caveat is in the body, as with KnowRL's 93% refusal rate.

Weight and Overstatement changes were checked as well:

| Paper | Change | Assessment |
|---|---|---|
| 2610.12989 | solid→incremental; none→overstatement about weak heads and modest benchmark effects | Both criticisms are body-supported; the rationale does not show that missing appendix content caused the downgrade. The appendix-only degeneration check disappears separately. |
| 2610.18262 | incremental→solid | B still carries the key original-skill failure (0–8.6%) and shortening restriction. No missing-appendix justification for the upgrade. |
| 2610.16426 | incremental→solid | B still calls the data synthetic, single-generator/single-policy, with label-informed signals. No appendix-specific explanation for the upgrade. |
| 2610.21200 | solid→incremental | B keeps the small-model, one-retriever, no-biological-validation limits. The difference is evaluation of novelty, not an identified appendix-derived change. |
| 2610.17301 | A's approver overstatement disappears | The simulated-principal and logged-only caveats are already in the body. The omitted exact n=87 is appendix-only but not independently material to B's stated claim. |
| 2610.19365 | A's human-audit and vanishing-gap overstatement disappears | Audit scope is body-available; the explicit 4B convergence at 800 examples is appendix-only and counted. |
| 2610.20221 | A's overstatement disappears | Structural-versus-semantic coverage and absent human verification remain in B. The separate 92.6% label-preservation result is lost and counted. |
| 2610.13928 | A's full-strength-versus-weakened-security overstatement disappears | Body-available; B still describes weakened settings, but drops the proof/full-strength distinction. |
| 2610.18415 | A's reduced-budget/paradigm-confounding overstatement disappears | Budget/seed limits survive in B; paradigm-confounding warning does not. Both are body-supported. |
| 2610.19452 | Template-dependence objection replaced by causal/influence objection | Both follow from body-described design; no appendix-only explanation. |
| 2610.20852 | B restricts causal ablations to binary data | Appendix contradicts it: both binary and multary, 40 examples each (L444). See below. |
| 2610.22127, 2610.18407 | A says none; B flags overstatement | Mixtral/correlational caveats and single-run/scale restrictions are available in the body/design. No established appendix-loss cause. |
| 2610.17457 | B drops exact FPR comparison from Overstatement | B retains the lower-Opus-FPR caveat in its Brief. The genuinely new loss is the appendix-only guarded adaptive result. |

Other Overstatement rewrites retain their substantive qualifications or substitute body-supported criticisms. These outputs alone do not justify attributing Weight fluctuations to appendix removal.

Separately, B introduces or strengthens these wrong/worse claims:

| Paper | B-only claim/problem | Source check and consequence |
|---|---|---|
| 2610.20852 | Overstatement says layer ablations were run “with the binary dataset.” | Appendix L444 explicitly uses **binary and multary**, 40 examples each. If intended as an exclusive restriction, B understates scope. This is a direct appendix contradiction. |
| 2610.20221 | “The judge is the same family used to select the configuration.” | Not supported. Body L288–313 describes GPT-5.4-guided selection with structural checks; appendix L683/L723/L763 calls the Gemma judge cross-family. B appears to conflate configuration selection with fidelity judging and invents a dependence criticism. |
| 2610.20993 | “The cyclic design is required for exactness.” | False. Body L118/L159/L264 explicitly permits **any position-balanced design**, including all K! permutations; appendix L849–883 proves the condition. Cyclic orderings are one sufficient design. |
| 2610.21261 | Reports the aggregate pairwise improvement without within-domain regressions | Appendix gives Law 0.5062→0.4155, Math 0.6244→0.5988, Economics 0.6558→0.6465, Engineering 0.8731→0.8689. This is an overgeneralization risk from omission, not a false aggregate number. Counted above. |
| 2610.19731 | Adds Attribute Maze recovery 87.1% without its 84.0% random-trunk comparator | Body L887–888 supplies both. B can suggest learned goal recovery where much can be relearned from a random trunk. Its Coin caveat does not repair this separate comparison. |
| 2610.18415 | “On-policy and offline training perform alike.” | The paper itself uses this wording, but the design compares different methods/data/rewards and reduced-budget single runs. A explicitly qualifies that causal conclusion; B does not. Body-available weakness, not an appendix-only contradiction. |
| 2610.16371 | Weight calls the framing direction “only modestly better than lexical baselines.” | Body L263–275: probe 0.594 beats learned TF-IDF (0.438–0.500) but loses to keyword matching (0.750). B's own Brief gives this correctly; its Weight is misleading if read as all lexical baselines. |
| 2610.16426 | Calls the manual label audit “small.” | The supplied text gives no sample size. This is unsupported precision about audit scale, rather than evidence that cutting hid a known denominator. |
| 2610.18407 | Says the single 1B model is “not a replication across scales.” | The single 337M→1B comparison is a limited cross-scale replication; it cannot establish broad scale invariance. A communicates this more accurately. B's criticism is too categorical, though it correctly flags limited replication. |
| 2610.21456 | Says gains on “four further directions,” then lists Hi↔En, En↔Fr, Hi→Ta, En→Ga | Body generalization tables include six directed translations in four listed groups. Minor counting/terminology problem; not an appendix-dependent error. |

I found no additional B-only factual contradiction or consequential worsening for the other 30 papers beyond the missing items already catalogued. Several additions improve B, including the correct SFT-relative BLEU interpretation in 2610.21456 and extra scope controls in other papers. A is not ground truth: for example, its blanket no-accessibility/behavior-test assertion in 2610.19365 is not established, and its compressed detector-F1 and judge-sensitivity ranges need care.

The strongest reasons to **keep the appendix** are:

1. **2610.14190 — a major method failure disappears.** B says the SAE changes task scores only slightly. The appendix shows that changing Gemma's SAE placement to layer 12 drives accuracy to **0.3315**, approximately **26 points lower**. A preserves the layer-sensitivity limit; B does not. The separate noisy-attention-supervision qualification also lives only in the appendix.
2. **2610.17457 — static detection hides adaptive failure.** B gives excellent detector F1 and large adaptive attack rates, but omits the experiment combining them: the detector lowers adaptive ASR only **93.55%→84.18%**. A includes it. That materially changes how a researcher would assess the defense.
3. **2610.12989 — a causal-output confound and its robustness check disappear together.** Only the appendix explains that SAE ablation increases degeneration and that the narrativity drop survives excluding degenerate generations. A carries both facts; B carries neither. Without them, the reader cannot distinguish a narrativity effect from generic output degradation or know that the authors checked.

These are source-verified appendix-only losses, not merely different word choices or choices of body-available numbers. The roughly one-third input saving does not meet the approved zero-material-loss condition on this set.
