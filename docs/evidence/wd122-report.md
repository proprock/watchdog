# WD-122 offline comparison report

Frozen sample: `wd122-sample.json` (sha256 `bddd7ed7bf6901876d088257b41d1b2cd7e7896352ebe09104fcd34a736c13c2`).

## Dataset

- Baseline report: `wd012-calibration.json`
- Baseline rule version: `wd-010.v1`
- Expanded rule version: `wd-010.v1`
- Sample sessions: 46
- Selection: {"all_eligible": true, "considered": 81, "eligible": 46, "min_events": 20, "provider": null, "seed": 12, "settle_hours": 2.0, "since": null, "skipped": {"below_min_events": 33, "not_settled": 2}, "strata": {"with_finding": 14, "without_finding_available": 32, "without_finding_drawn": 32}, "target": 40, "until": null}
- Provider mix: {"claude": 17, "codex": 29}
- Availability: {"events": 15042, "normalized_outcomes_unknown_with_response": 4454, "observation_gaps": 346, "provider_versions_unavailable": 15042, "surfaces_unknown": 15042, "tool_finishes": 4524, "tool_outcome_availability_unknown": 4524, "tool_responses_observed": 4454, "tool_responses_unavailable": 70}

## Before / after M2 findings

| rule | baseline observed | baseline TP / FP / n | baseline precision | expanded observed | expanded TP / FP / uncertain / n | unreviewed | expanded precision | change |
| --- | ---: | --- | ---: | ---: | --- | ---: | ---: | ---: |
| identical_error | 0 | 0 / 0 / 0 | null | 0 | 0 / 0 / 0 / 0 | 0 | null | unmeasured |
| repeated_test_failure | 0 | 0 / 0 / 0 | null | 0 | 0 / 0 / 0 / 0 | 0 | null | unmeasured |
| repeated_tool_outcome | 8 | 2 / 6 / 8 | 25.00% | 19 | 2 / 15 / 2 / 17 | 0 | 11.76% | -13.24 pp |

## Policy observations

- same_model_subagent_spawn: observed 16 time(s); excluded from M2 precision.

## Checkout diff oscillation

- diff_oscillation: observed 14 time(s); implicated session IDs {"01a07d94-c681-7693-a877-cebec978bf3a": 2, "01a07e28-900f-7b23-bc55-2f2571ce1bb7": 13, "01a07f91-bce6-7fe0-abe5-8ed2470049a4": 12, "01a07faa-fa6e-7fa1-9e26-63e718d30732": 12, "01a07faf-ecd8-7aa1-97af-48e706727e40": 11, "01a0822f-0745-7761-afff-51c448d60714": 1, "01a08236-2423-7570-909d-9967f4913195": 9, "01a0824c-df30-74d2-b88a-47f2a22f2229": 9, "01a084a9-7398-7293-ad52-fb6ff081f6b7": 7, "01a08805-66f3-76f1-91be-28fb974debc7": 6, "01a0881a-f104-7863-bfb8-78585c3f5a3a": 6, "01a089ba-13b8-7443-beed-23bb133c04cf": 1, "01a0e454-c911-70f0-93cb-a814c0b85ee0": 2, "01a0e47d-1b42-70d0-8558-c6fe07cbabb9": 1, "20707b47-52a2-4cc3-b9c8-7d85894f74c7": 2, "2bd19110-34d7-4ed3-bf5b-33eac9438429": 5, "42cd82e9-ce54-4f10-816f-93907fb085b8": 7, "4573c081-6eb5-46b6-913c-e8c8992bd3aa": 7, "8394af3e-3a4a-4c10-b898-a5ca715e8f67": 3, "8a5e5928-119f-4749-8cba-a32547428ef9": 7, "e8320fcb-0adf-4c3f-abe4-9987c78ecac0": 5, "ef62aae4-a871-4d0e-afe3-6bd534e8416a": 6, "f06870ad-049c-45be-abdf-a265f4f87cba": 3, "fea7a7d1-d6ee-43ca-b327-ef5ee33185a5": 7, "ff3e0390-d0fd-49c3-888e-7f62a6c30f9f": 7}. Checkout-scoped evidence is excluded from session precision.

## False-negative limits

- Reviewed slow/stuck sessions without an M2 finding: 4
- Silent sessions left unreviewed: 3
- Reviewed progress states: {"progress": 24, "slow": 4, "unknown": 4}

## Review examples

- `02b23abdab7f7275720b53c7d2302b88bd56003ffc8f32fcc4be5ea7a8b50c3b` evidence a1712d9a-2512-43d4-85f8-4648e8ef0fd5, 6aa2f774-157b-44c3-8367-c29fb09d3d0c, 9acc4672-abe6-4b67-a21f-ceb9b31dda52, 931684be-d9e7-4a7f-8f99-15c28ccc8020, 3a98b4ff-6a64-4f7f-b662-850183ca600a: Workflow reread as content changed.
- `0f0d672b6f186eeef2393a11aa792a49242f4f1103da17870fdf971bf8330b3a` evidence 77628034-de42-48df-93d7-8a1744c42e72, 887fbdae-af50-47b7-92ef-0b942c629021, 75651f71-715f-439e-8aa4-9f2f30e95e77: Three agent-list polls did not resolve stalled coordination.
- `175ce25c0982d4c792a02a19fc735713c09a3bf4b04bc633d674bfd3991d650f` evidence e19f14d9-f693-4d85-b662-f8b53ed69990, bb25d436-9c85-4661-8e1c-062e98cc2561, e6f1f0a2-4720-4742-82cd-59518d5a386f, 8a8c55cb-b826-4f42-8c62-2edcea870e0a: Short polls while pytest was running.
- `3529028097a9f86c7928a02cdb90b32a07cf1d09893b7772bf85011856ec62cb` evidence 50bf71ce-88e4-4a7c-8cc2-49699a875adf, ac861054-28a4-43db-9550-a9256c49bcb6, dc08183e-f4f1-48cc-9820-57773ee6f660, 1a8dfb3e-fb15-414e-8e10-19b4f8652aab, dbbaefbb-1eab-45e3-ad7a-c19f57c61c54, 14828cab-6f42-4546-8c2c-b93597f728fc: Broad Glob query recurred; blocked progress not established.
- `374816516e84498db0b32e77dd2f1b910ecee17669536854cec9c1608043c240` evidence 1d17e15e-a33c-4d07-bf55-0e48ddfac6e5, 1ab10d49-aede-4e75-8120-4c205567a0f7, 84c2be0e-060b-4d24-838b-76407386be03: Local guide reread across later turns and changed.
- `44351b3994c818735867adcb16dda2c20ed8f0379b5a1441f97610e5a1b0faf5` evidence ae035faf-1aea-4217-aae4-8304c9beb67f, f3d35599-c614-4481-8895-e7affd56ffbf, 071a2681-f64a-4892-906d-82bb6a479648: Pytest counts improved after edits across turns.
- `4b9dc1a5b44c6f8f01047b99bbe08768ebce691ac4fb24987c74a069ecdc5821` evidence 06d4b4dd-3348-4aab-bf73-f5d33ba1548b, f7df0c1a-4fbd-46fe-98d8-68e164f89121, af6a8a48-f4a5-4d20-ae37-72179212b635: Ruff checks progressed from access error through lint errors to clean.
- `7b4e10f73f49725c57b00aa1e24bdb65f643f6fd92f7ffac1629d7f76ad9df55` evidence bc2a36bf-d3e3-45d5-a9fc-446329d4dfc2, faa6e5b8-e4cf-49fb-b46c-84ffa0cf2e60, 9d597fdc-66a7-463e-b18a-3b55dc1e743e, fcbaa61c-0d03-4def-a114-357fe91619d8: Routine static index-status preflight.
- `7f2693d67000944783c0c97d87df094f66810e0b2e8aa387c48e214264a27d1d` evidence 9097f2d2-95b7-4e05-9f56-7176185bf6f3, c43f172e-5dfd-49ab-8fc3-fa373dfa9280, c67e73b1-7813-4ae6-839b-9f483db85fc0: Ruff failed, then passed after fix and verification.
- `8184a3581c5ed527a806ae2814d653b45558bae734e288f0369d78c2e65eb63a` evidence 146644f6-21d4-444d-a94d-cfa24cc3812d, 07f5845f-787b-488b-be3f-6c61e7c3c72c, bb4fe345-249a-4899-b24c-c253ab2f8a9e, a4cb5c51-0256-44d5-a92f-0c65e6d24293, 2ea67d4d-2f98-45cc-ac7b-de0c54c6fc43: Five unchanged wait polls without observed advancement.
- `a65a59e63c519ffe6478528b4b0e3dd51dcce8b533d3cf65ec09f9bce8cef200` evidence df2d126e-de78-4db4-ab66-fe731976916b, 228e95a4-4528-46c2-8aa7-3e03de6f9929, e3bedc00-49e4-4153-a27b-84916207bcdf: Environment probes changed output amid edits.
- `afc2fefacd5943f987f4dcb1fc939b949ba46edd1233ee8c8f88038b1a27d03c` evidence 217eb013-d6c4-4f72-852a-e22abb51fdc5, 657bf97d-a1e7-4a24-833f-b45680824fc4, 96cb2d2b-6d29-4614-829f-0908d9b21ef7: Poll responses showed test progress and final output.
- `c6aa671e7d837b29c6f537716aa2cfa0f9e1126a6bcc6becbb3eae403f1d4355` evidence 26327139-176f-49dc-85c2-f8590d4c5ad5, 848c1f82-9681-4a89-9713-b599f10af9bd, 444b361e-17e2-4138-9990-2ea06b1bee09: Index status after graph update; task context absent.
- `cb611c2f7fdc2ca4b2ce227bb3f679ab0e3724b9d5ba0ac7825fc8bf09eb12f6` evidence 0c3d2542-3c9a-4e2c-aaa8-12566a4d409b, 21512b22-48a2-4e04-ae8b-692d71ad4c5e, e265e3a3-775c-4eb6-853a-69b010a2eff0, 06a72637-1cd2-4396-b00b-8b623b693959, d8fa55e7-7303-4370-8b6b-d47385b72335, 97c5a116-9c91-4557-9e0f-b3ad3bcdc002: TODO reread across work phases and then changed.
- `cc214338c1d12db9410c11caaaa503686899afceb85a2d37d472a05e9b608d5b` evidence f3573564-c36a-4be2-a5db-0326fac6a23d, 57f7691f-961b-4c79-ac8c-472f77c2aeeb, 0d21dc9b-063f-49c8-92f4-8d8cc4b1c13a, 15eaf3dc-0b3d-454a-8e65-079f639eb7b9: TODO reread across turns and then changed.
- `ce92f3c4eba863f47f49ebad5bc27ecf0973e0cbd4ca0f3cc017fd4b1144b919` evidence 730b8290-9402-4a94-89cd-8b5c0bbf9977, 9a8ab7a4-4d7d-48cd-9dd2-80ab2cd8909a, 3b2b467a-0c0b-4624-82e3-666e136b5ca6, ac85307a-73a3-40d0-a48f-93b855a7fdfd: Build retries alternated errors and output around edits.
- `da7cd8c7d8b4dff369a2f28bbdf8631aac6d654d7b0a7b22cd35dbf577c5e907` evidence 9ba96841-5a58-4a18-afcd-60f2d4e51948, a5b37fd3-4b5c-405a-803e-0d3b37b37d2a, 16989cf1-8e49-4d7a-9369-ea9acd4d754a: File reread amid edits and writes.
- `e1775237e102a49187b8c2650a49e176dafb58472e8728c5227047c3e7998c9a` evidence 03a02d43-4c3d-48d5-81e4-b00e2042f311, f2ca3970-f611-4f58-804e-1c5884237e04, bcf7023e-80ef-41f7-aa63-20f071b4439d: Architecture section reread as content changed.
- `e9418344a544c796ad7182720244a303fbfac48230382f3a194b40ff5e0d245e` evidence 49eb87c2-3c0d-4230-80a4-f352b5d25c45, 4b8c584d-bf73-4955-8610-28e42edf2236, 406a3472-a15f-4317-b7be-605e11523dc1: Test file reread during ongoing work.
- `claude/042686d7-7e9b-4818-ace0-62e5fa9eaf8c` evidence c35a68a4-69f8-4d01-a6de-7a057b43f382, 34df2e29-f901-586c-8890-239c63deb2ec: Retained task trajectory indicates progress; reported completion is not independent verification.
- `claude/05f31f78-b7c7-41e2-8e1c-5ae70e61be54` evidence 5250cb25-65a8-49a2-b10a-0bd0d43421cf, bf04d9ba-9349-4572-9d7f-1ad2dbf88962: Retained task trajectory indicates progress; reported completion is not independent verification.
- `claude/275cfd15-a824-4eb8-ad18-b6b809e90c14` evidence 36f9256c-d821-4b6d-9313-01c20f79e8b7, f0c98cb0-22b4-538c-be42-6e9c0de77f48: WD-012 manual progress label.
- `claude/42cd82e9-ce54-4f10-816f-93907fb085b8` evidence 9e6e2795-3e0d-4068-bd40-bba24d3ce50d, abc0f1e1-a9cb-426b-89ac-59cf67ee8a7a: WD-012 manual progress label.
- `claude/6c812d9d-a85a-46a5-b0e6-bda373f790c8` evidence cb322735-01af-430b-a4d4-1e3e72275e4f, 2a13fc8a-a76c-4290-8304-77acc9dfabd8: WD-012 manual progress label.
- `claude/8394af3e-3a4a-4c10-b898-a5ca715e8f67` evidence 1be26d35-0988-4e93-be35-523edd1ff83d, b38ab15a-472a-42dd-964f-750159e1999f: Retained task trajectory indicates progress; reported completion is not independent verification.
- `claude/8a5e5928-119f-4749-8cba-a32547428ef9` evidence 78a64106-8c9a-4fb5-9e22-197cb477ca2e, 343e9c51-3a4f-4a71-ac82-43752329c031: WD-012 manual progress label.
- `claude/b5ee9e09-0903-46bf-a26c-c05095767820` evidence 5c24b122-d5a2-4754-b023-e905b036f20a, 2a406adc-18fb-4240-ac15-8a32e84c0cd7: Retained task trajectory indicates progress; reported completion is not independent verification.
- `claude/e8320fcb-0adf-4c3f-abe4-9987c78ecac0` evidence da081ff7-dbbb-49d4-bd21-1a3674720960, 105bce75-ecf3-5d5f-b827-44ced300aed6: Report produced, but repeated failures and capture gaps leave slow-progress judgment open.
- `claude/fea7a7d1-d6ee-43ca-b327-ef5ee33185a5` evidence b786cda9-8eb6-4a3c-b3f9-edb7d16848b8, a9e1d4c9-a310-4c01-a795-662775e0dd1d: WD-012 manual progress label.
- `codex/01a07d17-48be-71c3-981f-ae1db5e7bf99` evidence 46d0f532-a3ac-453d-afd4-017cd9d52162, 3a598edd-0887-5dd3-a573-8e68d68aca30: No WD-012 progress label.
- `codex/01a07d4a-630a-7033-8543-67a236ffa8ff` evidence 657d64f7-f822-4274-87ef-1da824c84730, 09e21dde-96af-5980-af3d-8ab257b8e528: No WD-012 progress label.
- `codex/01a07d94-c681-7693-a877-cebec978bf3a` evidence 44ef332f-6089-415a-8055-173ae80d25b8, 6b04fcbd-4435-4832-b219-1760b61b1fec: WD-012 manual progress label.
- `codex/01a07dbc-329e-7c31-9557-cfe097b14b8d` evidence b152f34f-94c1-4f78-848f-cd1de38e54e1, c6f71566-acb6-4990-a9cf-4f0fb1c43613: WD-012 manual progress label.
- `codex/01a07e04-373d-70a1-acc8-290c81a23d33` evidence 232362fe-85be-4142-a76d-389222e1c654, e48e27ef-79ce-4b15-b3bd-38bc4b9c96c2: WD-012 manual progress label.
- `codex/01a07e28-900f-7b23-bc55-2f2571ce1bb7` evidence 7dd8ad68-2331-40bc-ad0b-667d7feaba5d, b4e0f14e-faf8-5d87-9b0b-6f7acf15edab: WD-012 manual progress label.
- `codex/01a07f67-46a2-76d2-a495-20f1d2fa142a` evidence d1c5e54f-ed6d-42ce-86b1-1ff0e3a2f9f7, a91980f1-abb2-557c-b2a3-3970adb2de16: WD-012 manual progress label.
- `codex/01a07f73-550e-7213-ad08-f2090014c1fc` evidence 15047452-3278-4630-a1af-4824076bbf89, 28ea065f-1871-5dd6-bf8e-97073c1f6580: WD-012 manual progress label.
- `codex/01a07f8f-8592-7580-adeb-2312d3cf86de` evidence a59d8c0b-d067-4949-aeb7-85b404a6c5f3, b78d2890-4afd-5c39-8495-3ceff3eb14fa: WD-012 manual progress label.
- `codex/01a07f91-bce6-7fe0-abe5-8ed2470049a4` evidence 2b37afd7-34d2-42f1-afe4-851cfdec4d9b, 5d120066-5ca3-523e-9ba2-0778d365d702: WD-012 manual progress label.
- `codex/01a07faa-fa6e-7fa1-9e26-63e718d30732` evidence 045e74c3-0a4b-4ebd-aad9-1f0662423a72, e1d1355d-b4d1-483a-9d57-d41b29b13754: WD-012 manual progress label.
- `codex/01a0822f-0745-7761-afff-51c448d60714` evidence 5951c22a-92ea-46d0-9fd4-ade42d1dc827, a0b18198-37d8-5652-baa8-fd7e2ed30553: WD-012 manual progress label.
- `codex/01a08236-2423-7570-909d-9967f4913195` evidence df4d5fac-2a98-4f5d-912c-de6cb500b98d, c4c8845c-c7a9-557e-8979-1ccf077c20bc: WD-012 manual progress label.
- `codex/01a08255-497c-7540-a2f4-ceeca3b7e876` evidence 26f3c9c5-c996-4eeb-a9ea-58cc3b6a3df8, bfe9989f-28ff-54c9-ae8f-3ecfd32787ed: WD-012 manual progress label.
- `codex/01a082be-a3a1-7812-beb3-af9e42131bc4` evidence b8f48514-cda0-46db-bcdf-9f74cc90b144, fd42ed70-3d2b-4fe6-9269-c16698b79bc2: WD-012 manual progress label.
- `codex/01a08773-6d39-70c0-99e5-952755bbddeb` evidence 644490c2-b70e-4398-bdbc-f6ce817d5513, ca671661-bc55-5731-a7c9-7d896eb084d8: Retained task trajectory indicates progress; reported completion is not independent verification.
- `codex/01a08805-66f3-76f1-91be-28fb974debc7` evidence 2a03b7c7-4fdf-485d-a970-ee3e14bae2d5, 7440acfd-7d38-4673-a241-78baa22953f5: Retained task trajectory indicates progress; reported completion is not independent verification.
- `codex/01a0880a-8923-70e3-a23e-5341ca58622c` evidence 11214096-bbac-4188-9ef0-2767cd4b7780, a38dee7a-d09d-414c-83d4-65bd1999b7c3: Retained task trajectory indicates progress; reported completion is not independent verification.
- `codex/01a0881a-f104-7863-bfb8-78585c3f5a3a` evidence 73a4e2d9-011d-4535-97fc-af9e17a59528, 5db85054-e2d8-4e1d-8ae5-fcb4ea5b037a: Retained task trajectory indicates progress; reported completion is not independent verification.
- `codex/01a089ba-13b8-7443-beed-23bb133c04cf` evidence f5981fb3-5cc5-4e4c-8c48-26e99375bf42, 242433ca-8047-5d44-91dd-2d483bc9e9ec: Retained task trajectory indicates progress; reported completion is not independent verification.
- `codex/01a089ff-a05b-7851-bd36-81392c073785` evidence 05f9cfc6-5814-4051-baf8-24fa41e23075, a403f39c-bfb4-4d72-b580-42aeeb4c3d7b: Retained task trajectory indicates progress; reported completion is not independent verification.
- `codex/01a09b21-7809-7110-be03-0596a102ecb2` evidence eaf2e6d1-9a51-4157-ad74-c7a989a399a1, 355f0735-2efe-49ff-9b8d-bb1db8ae2c93: Initial prompt and turn starts absent; task origin unavailable.

## Guidance

Guidance remains closed unconditionally. WD-122 is an offline partial review; no rule is promoted by this report.

## Limitations

- Precision uses reviewed M2 findings only; unreviewed and uncertain findings remain unresolved.
- Recall is limited to reviewed slow or stuck sessions without an M2 finding; silent sessions without review remain unmeasured.
- Checkout diff oscillation is reported separately and never contributes to session-scoped precision.
