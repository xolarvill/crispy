from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.agents.runtime import AgentsRuntime
from app.providers.llm import GeneratedImage, GeneratedVideo, ImageGenResult, VideoGenResult
from app.schemas.contracts import (
    CopyImageBundle,
    CopyVariant,
    ImageAssetRef,
    PlanningBrief,
    ProductIntake,
    VariantCandidate,
    VariantSet,
    VideoBundle,
    VideoScriptItem,
    VideoScriptPack,
    VideoSegmentPlan,
)


class _FakeVideoProvider:
    def __init__(self) -> None:
        self.last_request = None
        self.last_extra = None

    def generate_video(self, request, *, api_base_url=None, api_key=None, extra=None):
        self.last_request = request
        self.last_extra = extra
        return VideoGenResult(
            model_used=request.model,
            videos=[GeneratedVideo(task_id="task-video-001", status="submitted")],
            task_id="task-video-001",
            status="submitted",
            raw_response={"status": "submitted"},
        )


class _FakeImageProvider:
    def __init__(self) -> None:
        self.last_extra = None

    def generate_image(self, request, *, api_base_url=None, api_key=None, extra=None):
        self.last_extra = extra
        return ImageGenResult(
            model_used=request.model,
            images=[GeneratedImage(task_id="task-image-001", status="submitted")],
            task_id="task-image-001",
            status="submitted",
            raw_response={"status": "submitted"},
        )


class _FakeCompletedVideoProvider:
    def __init__(self) -> None:
        self.requests = []

    def generate_video(self, request, *, api_base_url=None, api_key=None, extra=None):
        self.requests.append(request)
        payload = base64.b64encode((f"video-{len(self.requests)}".encode("utf-8") * 256)).decode("ascii")
        return VideoGenResult(
            model_used=request.model,
            videos=[GeneratedVideo(b64_data=payload, status="completed", raw_response={"last_frame_url": f"https://example.com/last-{len(self.requests)}.png"})],
            status="completed",
            raw_response={"status": "completed"},
        )


class _FakeCompletedVideoProviderWithoutLastFrameUrl:
    def __init__(self) -> None:
        self.requests = []

    def generate_video(self, request, *, api_base_url=None, api_key=None, extra=None):
        self.requests.append(request)
        payload = base64.b64encode((f"video-{len(self.requests)}".encode("utf-8") * 256)).decode("ascii")
        return VideoGenResult(
            model_used=request.model,
            videos=[GeneratedVideo(b64_data=payload, status="completed", raw_response={})],
            status="completed",
            raw_response={"status": "completed"},
        )


class _FakeRegistry:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get(self, _provider_name: str):
        return self.provider


def _patch_valid_segment_frame_sampling(monkeypatch):
    def fake_sample_video_frames(*, video_path, output_dir, prefix, count=3):
        from PIL import Image

        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for idx in range(count):
            path = output_dir / f"{prefix}_frame_{idx + 1:03d}.png"
            image = Image.new("RGB", (200, 200))
            for x in range(200):
                for y in range(200):
                    image.putpixel(xy=(x, y), value=((x * 3 + idx * 20) % 255, (y * 5) % 255, ((x + y) * 2) % 255))
            image.save(path, format="PNG")
            paths.append(str(path))
        return paths

    monkeypatch.setattr("app.agents.runtime.sample_video_frames", fake_sample_video_frames)


def test_apimart_gpt_image_defaults_to_submit_only():
    runtime = AgentsRuntime()
    fake_provider = _FakeImageProvider()
    runtime.providers = _FakeRegistry(fake_provider)

    result, provider_name, model_name = runtime._generate_image(
        fallback_provider="apimart",
        fallback_model="gpt-image-2",
        prompt="social ad image",
        size="1:1",
        runtime_config={
            "image": {
                "provider_name": "apimart",
                "model_name": "gpt-image-2",
                "api_base_url": "https://api.apimart.ai/v1",
                "api_key": "dummy",
            }
        },
    )

    assert provider_name == "apimart"
    assert model_name == "gpt-image-2"
    assert result.task_id == "task-image-001"
    assert fake_provider.last_extra["submit_only"] is True


def test_video_generation_traces_selected_provider_decision():
    runtime = AgentsRuntime()
    fake_provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(fake_provider)
    events = []

    result, provider_name, model_name = runtime._generate_video(
        fallback_provider="openai",
        fallback_model="gpt-4.1",
        prompt="show product",
        size="9:16",
        resolution="720p",
        duration_seconds=8,
        video_payload={"image_with_roles": [{"role": "first_frame", "image_url": "https://example.com/frame.png"}]},
        runtime_config={
            "video": {"provider_name": "apimart", "model_name": "doubao-seedance-2.0"},
            "trace_callback": lambda *args: events.append(args),
        },
    )

    assert result.task_id == "task-video-001"
    assert provider_name == "apimart"
    assert model_name == "doubao-seedance-2.0"
    event_type, _, payload = events[0]
    assert event_type == "provider_selection"
    assert payload["decision_type"] == "generation_provider_selection"
    assert payload["selected"] == "apimart/doubao-seedance-2.0"
    assert payload["options_considered"] == ["apimart/doubao-seedance-2.0", "openai/gpt-4.1"]
    assert payload["has_image_references"] is True


def test_human_integrity_instruction_only_applies_to_people_prompts():
    runtime = AgentsRuntime()

    assert runtime._human_integrity_instruction("Show a handbag on a marble table.") == ""
    assert "Human anatomy constraint" in runtime._human_integrity_instruction(
        "A real model wearing the dress walks slowly."
    )


def test_video_generation_prompt_includes_persona_contract():
    runtime = AgentsRuntime()
    runtime.providers = _FakeRegistry(_FakeVideoProvider())
    captured_prompts: list[str] = []

    def capture_chat(provider, model, prompt, runtime_config, **kwargs):
        captured_prompts.append(prompt)
        return '{"video_prompts":[{"variant_id":"V1","prompt":"Show the product in use."}]}', model, 0.0

    runtime._chat_complete = capture_chat
    runtime.run_video_generation(
        "persona-video-run",
        VideoScriptPack(
            scripts=[
                VideoScriptItem(
                    variant_id="V1",
                    hook="Hook",
                    script="Show the product.",
                )
            ],
            product_context={"product_name": "packing cube"},
        ),
        creative_specs={},
        provider="openai",
        model="gpt-4.1",
        runtime_config={
            "compiled_persona": {
                "lead_agent": {
                    "agent_name": "video_generation_agent",
                    "title": "Video Generation Agent",
                    "contract": {"mission": "Produce video assets from approved scripts."},
                },
                "collaborators": [],
            }
        },
    )

    assert "Persona Contract" in captured_prompts[0]
    assert "Video Generation Agent" in captured_prompts[0]


def test_video_scripting_uses_submitted_product_context_without_leash_defaults():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    variant_set = VariantSet(
        variants=[
            VariantCandidate(
                variant_id="V1",
                angle="organized packing",
                hook="Pack faster without messy leaks",
                message="Keeps travel-size bottles upright and separated.",
            )
        ]
    )
    intake = ProductIntake(
        product_name="travel toiletry bag",
        business_context={},
        asset_media_summary="Compact zip organizer with clear compartments and upright bottle sleeves.",
    )

    output = runtime.run_video_scripting(
        run_id="runtime-script-generic-product",
        variant_set=variant_set,
        intake=intake,
        business_context={
            "target_audience": "frequent travelers",
            "key_value_props": ["keeps bottles upright", "clear compartments"],
            "primary_cta": "Shop Now",
        },
        provider="openai",
        model="gpt-4.1",
        creative_specs={"tiktok_video_style": "ugc_demo", "video_duration_seconds": 12},
        pipeline_mode="tiktok_shop_video",
    )

    first_script = output.payload["scripts"][0]
    script_text = " ".join(
        [
            first_script["hook"],
            first_script["script"],
            *first_script["shot_list"],
            *(first_script.get("tiktok", {}) or {}).get("on_screen_text", []),
        ]
    ).lower()
    assert "travel toiletry bag" in script_text
    assert "keeps bottles upright" in script_text
    for banned in ("leash", "collar", "harness", "dog walk", "sidewalk", "trail walkers"):
        assert banned not in script_text


def test_divergence_adds_visual_proof_spec_for_concept_variants():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    output = runtime.run_divergence(
        run_id="runtime-divergence-visual-proof",
        planning=PlanningBrief(
            strategic_angles=["anti-pull control"],
            creative_director_plan={
                "scene_arc": [
                    {"scene_direction": "controlled dog walk with front chest leash redirection"},
                ]
            },
        ),
        variant_count=1,
        provider="openai",
        model="gpt-4.1",
    )

    variant = output.payload["variants"][0]
    spec = variant["visual_proof_spec"]
    assert spec["desired_scene"] == "controlled dog walk with front chest leash redirection"
    assert "the problem continuing unchanged" in spec["must_not_show"]
    assert output.payload["experiment_matrix"][0]["visual_proof_spec"] == spec


def test_divergence_outputs_customer_facing_hooks_without_variant_labels():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    output = runtime.run_divergence(
        run_id="runtime-divergence-customer-hooks",
        planning=PlanningBrief(
            strategic_angles=[
                "front clip redirects pulling during everyday walks with clear product proof",
                "padded breathable panels",
            ],
            creative_director_plan={
                "emotional_beats": ["relief", "confidence"],
                "scene_arc": [
                    {"scene_direction": "controlled dog walk with front chest leash redirection"},
                    {"scene_direction": "close view of padded mesh on the dog's chest"},
                ],
            },
        ),
        variant_count=2,
        provider="openai",
        model="gpt-4.1",
    )

    variants = output.payload["variants"]
    assert [variant["variant_id"] for variant in variants] == ["V1", "V2"]
    for variant in variants:
        assert not variant["hook"].startswith(("V1:", "V2:"))
        assert not variant["message"].startswith(("V1:", "V2:"))
        assert len(variant["hook"]) <= 72
    assert variants[0]["hook"] == "Front clip redirects pulling during everyday walks"
    assert variants[0]["message"] == (
        "Make buyers feel relief by proving front clip redirects pulling during everyday walks with clear product proof in the scene."
    )


def test_visual_proof_spec_uses_product_agnostic_recipes():
    runtime = AgentsRuntime()

    raincoat = runtime._variant_visual_proof_spec(
        VariantCandidate(
            variant_id="V1",
            angle="raincoat weather protection",
            hook="Show rain beading off the hooded dog raincoat",
            message="Keep the dog dry in rainy walks.",
        )
    )
    cup = runtime._variant_visual_proof_spec(
        VariantCandidate(
            variant_id="V2",
            angle="custom print paper cup branding",
            hook="Make the custom print panel visible at a cafe counter",
            message="Help events and shops show their brand in every drink photo.",
        )
    )
    brush = runtime._variant_visual_proof_spec(
        VariantCandidate(
            variant_id="V3",
            angle="shoe brush cleaning proof",
            hook="Show black bristles lifting dust from a shoe",
            message="Reveal a cleaner shoe surface after brushing.",
        )
    )

    assert raincoat["selling_point_type"] == "risk_removal"
    assert raincoat["creative_mode"] == "proof_demo"
    assert "visual implies the product failed to reduce the stated risk or problem" in raincoat["semantic_fail_conditions"]
    assert cup["selling_point_type"] == "identity_expression"
    assert cup["creative_mode"] == "creative_scene"
    assert "the identity-bearing detail" in cup["must_show"]
    assert brush["selling_point_type"] == "functional_proof"
    assert brush["creative_mode"] == "proof_demo"
    assert "the product causing the change" in brush["must_show"]


def test_divergence_creative_risk_adds_wildcard_leap_spec():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    output = runtime.run_divergence(
        run_id="runtime-divergence-creative-risk",
        planning=PlanningBrief(
            strategic_angles=["thin and light", "premium feel", "daily carry"],
            creative_director_plan={"scene_arc": [{"scene_direction": "product spins like a coin before reveal"}]},
        ),
        variant_count=3,
        creative_specs={"creative_risk_level": "wildcard"},
        provider="openai",
        model="gpt-4.1",
    )

    variants = output.payload["variants"]
    assert variants[0]["creative_leap_spec"]["style_risk_level"] == "bold"
    assert variants[-1]["creative_leap_spec"]["style_risk_level"] == "wildcard"
    assert variants[-1]["creative_leap_spec"]["creative_device"] == "impossible_physical_metaphor"
    assert output.payload["experiment_matrix"][-1]["creative_leap_spec"] == variants[-1]["creative_leap_spec"]


def test_wildcard_uses_product_agnostic_visual_proof_context():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    output = runtime.run_divergence(
        run_id="runtime-divergence-wildcard-context",
        planning=PlanningBrief(
            strategic_angles=["more controlled walks"],
            creative_director_plan={
                "creative_risk_level": "wildcard",
                "must_preserve_visuals": ["front chest D-ring", "orange strap"],
                "media_truth_summary": "Anti-pull dog harness with front chest D-ring and leash redirection.",
                "product_truth_contract": {"must_preserve": ["front chest D-ring"]},
                "scene_arc": [{"scene_direction": "end on product-forward frame with a simple purchase cue"}],
            },
        ),
        variant_count=1,
        provider="openai",
        model="gpt-4.1",
    )

    variant = output.payload["variants"][0]
    assert variant["visual_proof_spec"]["selling_point_type"] == "risk_removal"
    assert "front chest D-ring" not in variant["creative_leap_spec"]["opening_hook"]
    assert "line of force" not in variant["creative_leap_spec"]["opening_hook"]
    reasoning = variant["creative_leap_spec"]["conceptual_leap_reasoning"]
    assert reasoning["source_domains"] == ["physical analogy", "micro-story", "visual contrast"]
    assert reasoning["why_it_maps"]["product"] == "truth anchor"
    assert any("pure spectacle" in item for item in reasoning["rejected_metaphors"])


def test_video_scripting_writes_creative_leap_into_shots():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("not json", "stub-model", 0.0)

    output = runtime.run_video_scripting(
        run_id="runtime-script-creative-leap",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="custom brand print area",
                    hook="Make the custom print panel visible at a cafe counter",
                    message="The printed cup carries brand identity in real event photos.",
                    visual_proof_spec={
                        "selling_point_type": "identity_expression",
                        "proof_mechanism": "the printed cup visibly carries brand identity",
                        "must_show": ["custom print panel", "paper cup shape"],
                        "must_not_show": ["plain blank cup"],
                        "semantic_fail_conditions": ["visual hides customization"],
                    },
                    creative_leap_spec={
                        "creative_device": "visual_rhyme",
                        "shot_device_plan": [
                            "open with the cup print briefly acting like a tiny billboard in a real event photo",
                            "reveal the custom print panel on the paper cup",
                        ],
                    },
                )
            ]
        ),
        intake=ProductIntake(product_name="Custom print paper cup"),
        business_context={"target_audience": "cafes"},
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_duration_seconds": 8},
    )

    first_shot = output.payload["scripts"][0]["shot_list"][0]
    assert "creative leap:" in first_shot
    assert "tiny billboard" in first_shot


def test_video_scripting_carries_visual_proof_spec_into_shots():
    runtime = AgentsRuntime()
    captured_prompt: dict[str, str] = {}

    def fake_chat_complete(provider, model, prompt, runtime_config, **kwargs):
        captured_prompt["prompt"] = prompt
        return "not json", "stub-model", 0.0

    runtime._chat_complete = fake_chat_complete

    output = runtime.run_video_scripting(
        run_id="runtime-script-visual-proof",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="anti-pull control",
                    hook="Show the front chest D-ring redirecting leash tension",
                    message="Do not show a dog lunging forward as if pulling is uncontrolled.",
                    creative_leap_spec={
                        "creative_device": "bait_and_reveal",
                        "concept_metaphor": "a dog-walk scene first reads as chaos, then reveals controlled redirection",
                        "opening_hook": "start with leash tension close-up",
                        "reveal_structure": "reveal front D-ring control",
                        "must_not_break": ["product_truth_contract"],
                        "conceptual_leap_reasoning": {
                            "why_it_maps": {"front chest D-ring": "redirect point"},
                            "rejected_metaphors": ["tug-of-war because it makes the dog look uncontrolled"],
                        },
                    },
                )
            ]
        ),
        intake=ProductIntake(product_name="anti-pull dog harness"),
        business_context={"target_audience": "dog owners"},
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_duration_seconds": 8},
    )

    spec = output.payload["product_context"]["visual_proof_specs"]["V1"]
    first_shot = output.payload["scripts"][0]["shot_plan"][0]
    assert "visual_proof_specs" in captured_prompt["prompt"]
    assert "the problem continuing unchanged" in first_shot["first_frame"]["description"]
    assert f"avoid: {spec['semantic_fail_conditions'][-1]}" in first_shot["product_continuity_constraints"]


def test_video_scripting_splits_long_duration_into_segments():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    output = runtime.run_video_scripting(
        run_id="runtime-script-long-video",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="multi-scene styling",
                    hook="One dress, three moods",
                    message="Show the same dress across apartment, street, and evening scenes.",
                )
            ]
        ),
        intake=ProductIntake(
            product_name="olive satin dress",
            business_context={},
            asset_media_summary="Olive satin midi dress with twisted neckline and side ruching.",
        ),
        business_context={"target_audience": "fashion buyers", "primary_cta": "Shop the look"},
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16", "video_duration_seconds": 35},
        pipeline_mode="tiktok_shop_video",
    )

    segments = output.payload["scripts"][0]["segments"]
    assert [segment["duration_seconds"] for segment in segments] == [12.0, 12.0, 11.0]
    assert all(segment["duration_seconds"] <= 15 for segment in segments)
    assert [segment["shot_intent"] for segment in segments] == ["thumb_stop", "product_proof", "cta_packshot"]
    assert "one continuous ad" in segments[1]["motion_prompt"]
    assert "do not restart the hook" in segments[1]["motion_prompt"]
    assert "start_from_previous_tail_frame" in segments[1]["continuity_constraints"]
    assert "end_on_bridgeable_action_for_next_segment" in segments[0]["continuity_constraints"]
    assert segments[-1]["transition_to_next"] == "none"
    assert segments[0]["segment_contract"]["segment_id"] == "V1_S1"
    assert segments[0]["segment_contract"]["product_name"] == "olive satin dress"
    assert "product_truth_visible_in_first_frame" in segments[0]["segment_contract"]["preflight_checks"]
    assert "bridgeable_tail_frame" in segments[0]["segment_contract"]["preflight_checks"]
    assert "final_product_readable" in segments[-1]["segment_contract"]["preflight_checks"]


def test_video_scripting_adds_human_motion_risk_constraint_for_model_segments():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    output = runtime.run_video_scripting(
        run_id="runtime-script-human-motion-risk",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="model styling",
                    hook="A real model wears the dress",
                    message="Show the same model in a silver dress across scenes.",
                )
            ]
        ),
        intake=ProductIntake(
            product_name="silver dress",
            asset_media_summary="Reference image shows a real model wearing a silver dress.",
        ),
        business_context={"target_audience": "fashion buyers"},
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_duration_seconds": 32},
    )

    segments = output.payload["scripts"][0]["segments"]
    assert "avoid_high_risk_human_motion" in segments[1]["continuity_constraints"]


def test_planning_outputs_creative_director_and_production_plan():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("planning summary", "stub-model", 0.0)
    intake = ProductIntake(
        product_name="Robe Dress",
        market="US",
        locale="en-US",
        business_context={
            "audience": "US women shopping for elegant comfortable robe dresses",
            "brief": "real model, bedroom morning, cafe, boutique mirror, evening lounge",
        },
        manual_research_brief="32-second segmented video with real human model and varied scenes.",
        asset_media_summary="Reference image shows the robe dress silhouette and fabric drape.",
        visual_identity={"must_preserve_details": ["robe dress silhouette", "fabric drape"]},
    )

    output = runtime.run_planning(
        run_id="planning-director",
        intake=intake,
        gm_lessons=[],
        creative_specs={"video_duration_seconds": 32, "max_video_segment_seconds": 15},
        enable_research=False,
        provider="openai",
        model="gpt-4.1",
    )

    plan = output.payload["creative_director_plan"]
    production = output.payload["production_plan"]
    assert plan["scene_arc"][0]["beat"] == "thumb_stop"
    assert "robe dress silhouette" in plan["must_preserve_visuals"]
    assert any("bedroom morning" in scene for scene in plan["scene_hints"])
    assert production["segment_strategy"]["estimated_segment_count"] == 3
    assert output.payload["quality_gates"][0]["gate"] == "product_truth_lock"
    assert output.payload["creative_director_plan"]["product_truth_contract"]["must_preserve"][:2] == [
        "Robe Dress",
        "robe dress silhouette",
    ]


def test_prompt_grammar_flows_from_planning_to_video_script_and_storyboard():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("not json", "stub-model", 0.0)
    runtime._local_media_qa = lambda **kwargs: {"status": "pass", "score": 100, "flags": [], "checks": []}
    runtime._generate_image = lambda **kwargs: (
        ImageGenResult(
            model_used="image-model",
            images=[
                GeneratedImage(
                    b64_json="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
                )
            ],
        ),
        "stub-image-provider",
        "stub-image-model",
    )

    intake = ProductIntake(
        product_name="Anti-pull dog harness",
        manual_research_brief=(
            "TikTok Shop 12s video. Style reference: candid handheld early-2000s home-video realism. "
            "Show a real dog-walk proof moment, not generic cute pet lifestyle."
        ),
        asset_media_summary=(
            "White-background product image of an anti-pull dog harness with orange, teal-blue, "
            "and beige woven straps, black D-rings, dark blue buckle parts, and a yellow adjustment buckle."
        ),
        business_context={
            "target_audience": ["urban dog owners whose dogs pull on walks"],
            "key_value_props": ["front chest D-ring redirects pulling"],
            "primary_cta": "Shop on TikTok",
            "prohibited_claims": ["no guaranteed instant behavior correction"],
        },
        product_truth_contract={
            "must_preserve": ["orange woven strap", "teal-blue vertical strap", "black metal D-rings"],
            "forbidden_changes": ["do_not_remove_d_rings_or_buckle"],
        },
    )
    creative_specs = {"video_size": "9:16", "video_duration_seconds": 12, "tiktok_video_style": "ugc_demo"}

    planning_output = runtime.run_planning(
        run_id="prompt-grammar-flow",
        intake=intake,
        gm_lessons=[],
        creative_specs=creative_specs,
        enable_research=False,
        provider="openai",
        model="gpt-4.1",
    )
    planning = PlanningBrief(**{key: planning_output.payload[key] for key in PlanningBrief.model_fields})
    prompt_grammar = planning_output.payload["creative_director_plan"]["prompt_grammar"]

    assert "autofocus hunting" in prompt_grammar["camera_style"]
    assert "leash jingles" not in prompt_grammar["audio_plan"]
    assert "front chest D-ring" not in planning.creative_director_plan["scene_arc"][0]["scene_direction"]
    scene_directions = [item["scene_direction"] for item in planning.creative_director_plan["scene_arc"]]
    assert len(scene_directions) == len(set(scene_directions))

    variant_set = VariantSet(
        variants=[
            VariantCandidate(
                variant_id="V1",
                angle="front chest D-ring redirects pulling",
                hook="Show the front chest D-ring redirecting leash tension",
                message="Make the controlled walking posture visible.",
            )
        ]
    )
    script_output = runtime.run_video_scripting(
        run_id="prompt-grammar-flow",
        variant_set=variant_set,
        intake=intake,
        business_context=intake.business_context,
        provider="openai",
        model="gpt-4.1",
        creative_specs=creative_specs,
        pipeline_mode="tiktok_shop_video",
        planning=planning,
    )
    first_shot = script_output.payload["scripts"][0]["shot_plan"][0]
    first_shot_text = script_output.payload["scripts"][0]["shot_list"][0]

    assert "Prompt grammar" not in first_shot_text
    assert "product proof: show" in first_shot_text
    assert len(first_shot_text) < 500
    assert "autofocus hunting" in first_shot["motion_description"]
    assert "natural ambient sound only" in first_shot["audio_description"]

    storyboard_output = runtime.run_storyboard_image_generation(
        run_id="prompt-grammar-flow",
        script_pack=VideoScriptPack(**{key: script_output.payload[key] for key in VideoScriptPack.model_fields}),
        provider="openai",
        model="gpt-4.1",
        creative_specs=creative_specs,
        planning=planning,
    )
    first_frame_prompt = storyboard_output.payload["frames"][0]["prompt"]

    assert "autofocus hunting" in first_frame_prompt
    assert "natural ambient sound only" in first_frame_prompt
    assert "Visual proof spec: Visual proof:" in first_frame_prompt
    assert "mode proof_demo" in first_frame_prompt


def test_video_scripting_carries_product_truth_contract():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("", "stub-model", 0.0)
    intake = ProductIntake(
        product_name="silver dress",
        asset_media_summary="Reference image shows a real model wearing a silver dress.",
        visual_identity={
            "colors": ["silver"],
            "materials": ["fabric"],
            "must_preserve_details": ["silver color", "twisted neckline"],
        },
    )

    output = runtime.run_video_scripting(
        run_id="truth-contract-script",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="fit proof",
                    hook="Show the neckline clearly",
                    message="Keep the same dress across scenes.",
                )
            ]
        ),
        intake=intake,
        business_context={"target_audience": "fashion buyers"},
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_duration_seconds": 20},
    )

    contract = output.payload["product_context"]["product_truth_contract"]
    assert contract["colors"] == ["silver"]
    assert "twisted neckline" in contract["must_preserve"]
    assert "do_not_add_remove_or_replace_product_parts" in contract["forbidden_changes"]


def test_copy_image_generation_reuses_intake_media_summary_for_reference_images(tmp_path):
    runtime = AgentsRuntime()
    chat_calls = []

    def fake_chat_complete(*args, **kwargs):
        chat_calls.append(kwargs)
        return ("copy hint", "text-model", 0.0)

    def fake_generate_image(*, prompt, reference_image_urls=None, **kwargs):
        assert "blue upper panel and black padded lower section" in prompt
        assert "do_not_change_product_color_material_or_shape" in prompt
        assert reference_image_urls
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": [object()], "model_used": "image-model"})(),
            "stub-image-provider",
            "stub-image-model",
        )

    ref = tmp_path / "harness.png"
    ref.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"))
    runtime._chat_complete = fake_chat_complete
    runtime._generate_image = fake_generate_image
    runtime._materialize_generated_image = lambda selected: (b"image-bytes" * 256, "b64_json")
    runtime._local_media_qa = lambda **kwargs: {"status": "pass", "score": 100, "flags": [], "checks": []}

    output = runtime.run_copy_image_generation(
        run_id="copy-summary-reuse",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show the harness clearly.",
                )
            ]
        ),
        intake=ProductIntake(
            product_name="blue pet harness",
            asset_media_summary="blue upper panel and black padded lower section",
            image_references=[{"uri": str(ref)}],
            visual_identity={"must_preserve_details": ["blue upper panel", "black padded lower section"]},
        ),
        business_context={"target_audience": "dog owners", "primary_cta": "Shop Now"},
        creative_specs={},
        market="US",
        locale="en-US",
        provider="deepseek",
        model="deepseek-v4-pro",
    )

    assert len(chat_calls) == 1
    assert not chat_calls[0].get("image_urls")
    assert output.payload["image_assets"][0]["prompt"].find("reference_analysis_failed") == -1
    assert output.payload["image_assets"][0]["image_asset_contract"]["status"] == "pass"
    assert output.payload["image_assets"][0]["image_asset_contract"]["blocking"] is False


def test_copy_image_generation_blocks_placeholder_assets():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("copy hint", "text-model", 0.0)
    runtime._generate_image = lambda **kwargs: (
        type("ImageResult", (), {"estimated_cost": 0.0, "images": [], "model_used": "image-model"})(),
        "stub-image-provider",
        "stub-image-model",
    )

    with pytest.raises(RuntimeError, match="image provider produced no reviewable assets"):
        runtime.run_copy_image_generation(
            run_id="copy-placeholder-blocked",
            variant_set=VariantSet(
                variants=[
                    VariantCandidate(
                        variant_id="V1",
                        angle="secure fit",
                        hook="Clip in confidence",
                        message="Show the harness clearly.",
                    )
                ]
            ),
            intake=ProductIntake(
                product_name="blue pet harness",
                asset_media_summary="blue upper panel and black padded lower section",
            ),
            business_context={"target_audience": "dog owners", "primary_cta": "Shop Now"},
            creative_specs={},
            market="US",
            locale="en-US",
            provider="deepseek",
            model="deepseek-v4-pro",
        )


def test_copy_image_generation_continues_after_placeholder_asset():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("copy hint", "text-model", 0.0)
    calls = {"count": 0}

    def generate_image(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return (
                type("ImageResult", (), {"estimated_cost": 0.0, "images": [], "model_used": "image-model"})(),
                "stub-image-provider",
                "stub-image-model",
            )
        return (
            ImageGenResult(
                model_used="image-model",
                images=[GeneratedImage(task_id="image-task-2", status="submitted", raw_response={"status": "submitted"})],
                task_id="image-task-2",
                status="submitted",
                raw_response={"status": "submitted"},
            ),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = generate_image

    output = runtime.run_copy_image_generation(
        run_id="copy-placeholder-continues",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show the harness clearly.",
                ),
                VariantCandidate(
                    variant_id="V2",
                    angle="front detail",
                    hook="See the D-ring up close",
                    message="Show the harness hardware clearly.",
                ),
            ]
        ),
        intake=ProductIntake(product_name="blue pet harness"),
        business_context={"target_audience": "dog owners", "primary_cta": "Shop Now"},
        creative_specs={},
        market="US",
        locale="en-US",
        provider="deepseek",
        model="deepseek-v4-pro",
    )

    assert calls["count"] == 2
    assert output.payload["image_assets"][0]["image_asset_contract"]["blocking"] is True
    assert output.payload["image_assets"][1]["source"] == "external_task_pending"


def test_copy_image_generation_blocks_all_provider_error_assets():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("copy hint", "text-model", 0.0)

    def fail_image(**kwargs):
        raise RuntimeError("request failed for all endpoint candidates")

    runtime._generate_image = fail_image

    with pytest.raises(RuntimeError, match="image provider produced no reviewable assets"):
        runtime.run_copy_image_generation(
            run_id="copy-provider-error-asset",
            variant_set=VariantSet(
                variants=[
                    VariantCandidate(
                        variant_id="V1",
                        angle="formal event",
                        hook="Silver dress moment",
                        message="Show the dress clearly.",
                    )
                ]
            ),
            intake=ProductIntake(
                product_name="silver dream dress",
                asset_media_summary="light silver-grey midi dress with ruffled neckline",
            ),
            business_context={"primary_cta": "Shop Now"},
            creative_specs={},
            market="US",
            locale="en-US",
            provider="deepseek",
            model="deepseek-v4-pro",
        )


def test_copy_image_generation_keeps_mixed_provider_error_as_failed_asset():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("copy hint", "text-model", 0.0)
    calls = {"count": 0}

    def generate_or_fail(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return (
                ImageGenResult(
                    model_used="image-model",
                    images=[GeneratedImage(task_id="image-task-1", status="submitted", raw_response={"status": "submitted"})],
                    task_id="image-task-1",
                    status="submitted",
                    raw_response={"status": "submitted"},
                ),
                "stub-image-provider",
                "stub-image-model",
            )
        raise RuntimeError("request failed for all endpoint candidates")

    runtime._generate_image = generate_or_fail

    output = runtime.run_copy_image_generation(
        run_id="copy-mixed-provider-error-asset",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="formal event",
                    hook="Silver dress moment",
                    message="Show the dress clearly.",
                ),
                VariantCandidate(
                    variant_id="V2",
                    angle="fit detail",
                    hook="A closer look at the ruffles",
                    message="Show neckline and fabric clearly.",
                ),
            ]
        ),
        intake=ProductIntake(
            product_name="silver dream dress",
            asset_media_summary="light silver-grey midi dress with ruffled neckline",
        ),
        business_context={"primary_cta": "Shop Now"},
        creative_specs={},
        market="US",
        locale="en-US",
        provider="deepseek",
        model="deepseek-v4-pro",
    )

    image = output.payload["image_assets"][1]
    flags = image["visual_qa"]["flags"]
    assert image["source"] == "generation_error"
    assert image["error"] == "request failed for all endpoint candidates"
    assert flags == ["visual_qa_generation_error"]
    assert "visual_qa_decode_error" not in flags
    assert "visual_qa_product_truth_color_mismatch" not in flags
    assert image["image_asset_contract"]["blocking"] is True
    assert image["image_asset_contract"]["flags"] == ["media_gate_generation_error"]


def test_copy_image_generation_exposes_pending_image_task():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("copy hint", "text-model", 0.0)
    runtime._generate_image = lambda **kwargs: (
        ImageGenResult(
            model_used="image-model",
            images=[GeneratedImage(task_id="image-task-1", status="submitted", raw_response={"status": "submitted"})],
            task_id="image-task-1",
            status="submitted",
            raw_response={"status": "submitted"},
        ),
        "stub-image-provider",
        "stub-image-model",
    )

    output = runtime.run_copy_image_generation(
        run_id="copy-pending-image-task",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show the harness clearly.",
                )
            ]
        ),
        intake=ProductIntake(product_name="blue pet harness"),
        business_context={"target_audience": "dog owners", "primary_cta": "Shop Now"},
        creative_specs={},
        market="US",
        locale="en-US",
        provider="deepseek",
        model="deepseek-v4-pro",
        runtime_config={"force_regenerate": True},
    )

    image = output.payload["image_assets"][0]
    assert image["source"] == "external_task_pending"
    assert image["external_task_id"] == "image-task-1"
    assert image["generation_status"] == "submitted"
    assert image["external_task_state"]["task_id"] == "image-task-1"
    assert image["external_task_state"]["status"] == "submitted"
    assert image["external_task_state"]["poll_count"] == 0
    assert image["image_asset_contract"]["blocking"] is False
    assert "media_gate_asset_processing" in image["image_asset_contract"]["flags"]


def test_copy_image_generation_uses_variant_visual_proof_spec():
    runtime = AgentsRuntime()
    captured_prompts: list[str] = []
    runtime._chat_complete = lambda *args, **kwargs: ("copy hint", "text-model", 0.0)
    runtime._local_media_qa = lambda **kwargs: {"status": "pass", "score": 100, "flags": [], "checks": []}
    runtime._materialize_generated_image = lambda selected: (b"image-bytes" * 256, "b64_json")

    def fake_generate_image(*, prompt, **kwargs):
        captured_prompts.append(prompt)
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": [object()], "model_used": "image-model"})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    output = runtime.run_copy_image_generation(
        run_id="copy-visual-proof",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="anti-pull control",
                    hook="Show the front chest D-ring redirecting leash tension",
                    message="Do not show a dog lunging forward as if pulling is uncontrolled.",
                    creative_leap_spec={
                        "creative_device": "bait_and_reveal",
                        "concept_metaphor": "a dog-walk scene first reads as chaos, then reveals controlled redirection",
                        "opening_hook": "start with leash tension close-up",
                        "reveal_structure": "reveal front D-ring control",
                        "must_not_break": ["product_truth_contract"],
                        "conceptual_leap_reasoning": {
                            "why_it_maps": {"front chest D-ring": "redirect point"},
                            "rejected_metaphors": ["tug-of-war because it makes the dog look uncontrolled"],
                        },
                    },
                )
            ]
        ),
        intake=ProductIntake(product_name="anti-pull dog harness"),
        business_context={"target_audience": "dog owners", "primary_cta": "Shop Now"},
        creative_specs={"image_direction": "Keep the leash contact point in side light."},
        market="US",
        locale="en-US",
        provider="deepseek",
        model="deepseek-v4-pro",
        runtime_config={"force_regenerate": True},
    )

    assert "Variant visual proof spec" in captured_prompts[0]
    assert "Keep the leash contact point in side light." in captured_prompts[0]
    assert "Creative leap: device bait_and_reveal" in captured_prompts[0]
    assert "the problem continuing unchanged" in captured_prompts[0]
    assert output.payload["image_assets"][0]["visual_proof_spec"]["semantic_fail_conditions"]
    assert output.payload["image_assets"][0]["creative_leap_spec"]["creative_device"] == "bait_and_reveal"
    assert "mapping" in captured_prompts[0]


def test_copy_image_generation_diversifies_visual_proof_styles():
    runtime = AgentsRuntime()
    captured_prompts: list[str] = []
    runtime._chat_complete = lambda *args, **kwargs: ("copy hint", "text-model", 0.0)
    runtime._local_media_qa = lambda **kwargs: {"status": "pass", "score": 100, "flags": [], "checks": []}
    runtime._materialize_generated_image = lambda selected: (b"image-bytes" * 256, "b64_json")

    def fake_generate_image(*, prompt, **kwargs):
        captured_prompts.append(prompt)
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": [object()], "model_used": "image-model"})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    output = runtime.run_copy_image_generation(
        run_id="copy-diverse-proof-styles",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(variant_id="V1", angle="front clip redirects pulling", hook="Calmer walks", message="Show control."),
                VariantCandidate(variant_id="V2", angle="padded breathable panels", hook="Comfort close-up", message="Show padding."),
                VariantCandidate(variant_id="V3", angle="reflective trim for evening walks", hook="Evening confidence", message="Show visibility."),
                VariantCandidate(variant_id="V4", angle="anti-pull comparison", hook="Before and after", message="Show the difference."),
            ]
        ),
        intake=ProductIntake(product_name="anti-pull dog harness"),
        business_context={"target_audience": "dog owners", "primary_cta": "Shop Now"},
        creative_specs={},
        market="US",
        locale="en-US",
        provider="deepseek",
        model="deepseek-v4-pro",
        runtime_config={"force_regenerate": True},
    )

    styles = [
        image["visual_proof_spec"]["proof_style"]["name"]
        for image in output.payload["image_assets"]
    ]
    assert styles == [
        "single in-use moment",
        "product detail close-up",
        "lifestyle outcome",
        "comparison or callout",
    ]
    assert "no split panels or symbolic icons" in captured_prompts[0]
    assert "before/after, callout, arrow, checkmark, or infographic treatment is allowed" in captured_prompts[3]
    assert "infographic treatment is allowed" not in "\n".join(captured_prompts[:3])


def test_copy_image_generation_includes_qa_repair_prompt():
    runtime = AgentsRuntime()
    captured_prompts: list[str] = []
    runtime._chat_complete = lambda *args, **kwargs: ("copy hint", "text-model", 0.0)
    runtime._local_media_qa = lambda **kwargs: {"status": "pass", "score": 100, "flags": [], "checks": []}
    runtime._materialize_generated_image = lambda selected: (b"image-bytes" * 256, "b64_json")

    def fake_generate_image(*, prompt, **kwargs):
        captured_prompts.append(prompt)
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": [object()], "model_used": "image-model"})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    runtime.run_copy_image_generation(
        run_id="copy-qa-repair",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show the harness clearly.",
                )
            ]
        ),
        intake=ProductIntake(product_name="blue pet harness"),
        business_context={"target_audience": "dog owners", "primary_cta": "Shop Now"},
        creative_specs={},
        market="US",
        locale="en-US",
        provider="deepseek",
        model="deepseek-v4-pro",
        runtime_config={
            "force_regenerate": True,
            "qa_repair": {"prompt": "Regeneration repair instructions: restore product visibility."},
        },
    )

    assert "Regeneration repair instructions: restore product visibility." in captured_prompts[0]


def test_video_scripting_carries_planning_director_plan_into_fallback():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)
    planning = PlanningBrief(
        strategic_angles=["travel order"],
        creative_director_plan={
            "emotional_beats": ["relief", "confidence"],
            "must_preserve_visuals": ["clear compartments", "upright bottle sleeves"],
            "scene_arc": [
                {"beat": "thumb_stop", "scene_direction": "messy hotel counter transforms into organized kit"},
                {"beat": "proof", "scene_direction": "close-up bottles stay upright inside clear sleeves"},
            ],
        },
        production_plan={"segment_strategy": {"estimated_segment_count": 1}},
        quality_gates=[{"gate": "product_truth_lock"}],
    )

    output = runtime.run_video_scripting(
        run_id="runtime-script-director-plan",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="travel order",
                    hook="Pack without leaks",
                    message="Keep bottles upright in transit.",
                )
            ]
        ),
        intake=ProductIntake(
            product_name="travel toiletry bag",
            asset_media_summary="Compact organizer with clear compartments.",
        ),
        business_context={"target_audience": "frequent travelers", "primary_cta": "Shop Now"},
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_duration_seconds": 12},
        planning=planning,
    )

    first_script = output.payload["scripts"][0]
    assert output.payload["director_strategy"]["creative_director_plan"]["emotional_beats"] == ["relief", "confidence"]
    assert "messy hotel counter" in first_script["shot_list"][0]
    assert "clear compartments" in first_script["shot_list"][0]
    assert "Cold open" in first_script["script"]
    assert "overlay:" in first_script["shot_list"][0]
    assert first_script["shot_plan"][0]["first_frame"]["description"] == first_script["shot_list"][0]


def test_video_generation_stitches_completed_segments(monkeypatch):
    runtime = AgentsRuntime()
    provider = _FakeCompletedVideoProvider()
    runtime.providers = _FakeRegistry(provider)
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    def fake_extract_last_frame(*, video_path, output_path):
        output_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"))
        return str(output_path)

    def fake_stitch(*, video_paths, output_path):
        output_path.write_bytes(b"stitched-video" * 256)
        return str(output_path)

    monkeypatch.setattr("app.agents.runtime.extract_last_video_frame", fake_extract_last_frame)
    monkeypatch.setattr("app.agents.runtime.stitch_video_files", fake_stitch)
    _patch_valid_segment_frame_sampling(monkeypatch)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="One dress, three moods",
                script="A segmented long-form short ad.",
                segments=[
                    {
                        "segment_id": "V1_S1",
                        "variant_id": "V1",
                        "duration_seconds": 12,
                        "first_frame_prompt": "apartment reveal",
                        "last_frame_prompt": "walking out",
                        "motion_prompt": "model smiles in apartment",
                        "segment_contract": {"must_preserve": ["olive satin dress"], "preflight_checks": ["product_truth_visible_in_first_frame"]},
                    },
                    {
                        "segment_id": "V1_S2",
                        "variant_id": "V1",
                        "duration_seconds": 12,
                        "first_frame_prompt": "street",
                        "last_frame_prompt": "cafe",
                        "motion_prompt": "model crosses street",
                    },
                    {
                        "segment_id": "V1_S3",
                        "variant_id": "V1",
                        "duration_seconds": 11,
                        "first_frame_prompt": "evening",
                        "last_frame_prompt": "cta",
                        "motion_prompt": "model enters evening scene",
                    },
                ],
            )
        ],
        product_context={"product_name": "olive satin dress"},
        generation_spec={"size": "9:16", "resolution": "720p", "duration": 35},
    )

    output = runtime.run_video_generation(
        run_id="runtime-video-segments",
        script_pack=script_pack,
        creative_specs={"video_size": "9:16", "video_duration_seconds": 35},
        provider="openai",
        model="gpt-4.1",
        runtime_config={"video": {"provider_name": "fake", "model_name": "fake-video"}, "force_regenerate": True},
    )

    video = output.payload["videos"][0]
    assert [request.duration_seconds for request in provider.requests] == [12, 12, 11]
    assert all(request.return_last_frame is True for request in provider.requests)
    assert provider.requests[1].image_with_roles == [{"url": "https://example.com/last-1.png", "role": "first_frame"}]
    assert provider.requests[2].image_with_roles == [{"url": "https://example.com/last-2.png", "role": "first_frame"}]
    assert video["segments"][1]["reference_mode"] == "first_frame"
    assert video["segments"][1]["reference_image_count"] == 1
    assert "only opening hook" in provider.requests[0].prompt
    assert "Segment contract" in provider.requests[0].prompt
    assert video["segments"][0]["segment_contract"]["must_preserve"] == ["olive satin dress"]
    assert "Continue the exact action from the supplied first_frame reference" in provider.requests[1].prompt
    assert "do not restart with a new intro" in provider.requests[2].prompt
    assert video["source"] == "stitched_segments"
    assert video["stitch_preflight"]["status"] == "pass"
    assert video["segment_ledger"]["status"] == "completed"
    assert video["segment_ledger"]["segments"][0]["segment_id"] == "V1_S1"
    assert video["segment_ledger"]["segments"][0]["tail_frame"]
    assert video["segment_ledger"]["segments"][0]["contract"]["must_preserve"] == ["olive satin dress"]
    assert video["segments"][0]["segment_frame_qa"]["status"] == "pass"
    assert video["segment_ledger"]["segments"][0]["segment_frame_qa_status"] == "pass"
    assert video["duration_seconds"] == 35.0
    assert len(video["segments"]) == 3
    assert video["video_uri"].endswith("V1_stitched.mp4")


def test_segmented_video_generation_resumes_from_failed_segment(monkeypatch, tmp_path):
    runtime = AgentsRuntime()
    provider = _FakeCompletedVideoProvider()
    runtime.providers = _FakeRegistry(provider)
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    preserved = tmp_path / "V1_S1.mp4"
    preserved.write_bytes(b"\x00\x00\x00\x20ftypisom" + (b"1" * 2048))

    def fake_extract_last_frame(*, video_path, output_path):
        output_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"))
        return str(output_path)

    def fake_stitch(*, video_paths, output_path):
        output_path.write_bytes(b"stitched-video" * 256)
        return str(output_path)

    monkeypatch.setattr("app.agents.runtime.extract_last_video_frame", fake_extract_last_frame)
    monkeypatch.setattr("app.agents.runtime.stitch_video_files", fake_stitch)
    _patch_valid_segment_frame_sampling(monkeypatch)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="One dress, three moods",
                script="A segmented ad.",
                segments=[
                    {
                        "segment_id": "V1_S1",
                        "variant_id": "V1",
                        "duration_seconds": 8,
                        "motion_prompt": "start",
                        "segment_contract": {"must_preserve": ["olive satin dress"]},
                    },
                    {
                        "segment_id": "V1_S2",
                        "variant_id": "V1",
                        "duration_seconds": 8,
                        "motion_prompt": "continue",
                        "segment_contract": {"must_preserve": ["olive satin dress"]},
                    },
                    {
                        "segment_id": "V1_S3",
                        "variant_id": "V1",
                        "duration_seconds": 8,
                        "motion_prompt": "finish",
                        "segment_contract": {"must_preserve": ["olive satin dress"]},
                    },
                ],
            )
        ],
        product_context={"product_name": "olive satin dress"},
        generation_spec={"size": "9:16", "resolution": "720p", "duration": 24},
    )

    output = runtime.run_video_generation(
        run_id="runtime-video-resume-segment",
        script_pack=script_pack,
        creative_specs={"video_size": "9:16", "video_duration_seconds": 24},
        provider="openai",
        model="gpt-4.1",
        runtime_config={
            "video": {"provider_name": "fake", "model_name": "fake-video"},
            "force_regenerate": True,
            "resume_video_payload": {
                "variant_id": "V1",
                "segments": [
                    {
                        "segment_id": "V1_S1",
                        "segment_index": 0,
                        "video_uri": str(preserved),
                        "generation_status": "completed",
                        "last_frame_url": "https://example.com/s1-tail.png",
                        "segment_contract": {"must_preserve": ["olive satin dress"]},
                    }
                ],
                "segment_ledger": {
                    "first_blocked_segment_id": "V1_S2",
                    "segments": [
                        {"segment_id": "V1_S1", "status": "completed"},
                        {"segment_id": "V1_S2", "status": "needs_regeneration"},
                    ],
                },
            },
        },
    )

    video = output.payload["videos"][0]
    assert [request.duration_seconds for request in provider.requests] == [8, 8]
    assert video["segments"][0]["segment_id"] == "V1_S1"
    assert video["segments"][0]["video_uri"] == str(preserved)
    assert video["segments"][1]["segment_id"] == "V1_S2"
    assert video["segments"][1]["reference_mode"] == "first_frame"
    assert provider.requests[0].image_with_roles == [{"url": "https://example.com/s1-tail.png", "role": "first_frame"}]
    assert video["segment_ledger"]["status"] == "completed"


def test_stitch_preflight_blocks_missing_segment_contract():
    runtime = AgentsRuntime()

    preflight = runtime._stitch_preflight(
        segments=[
            VideoSegmentPlan(segment_id="V1_S1", variant_id="V1", duration_seconds=8),
            VideoSegmentPlan(segment_id="V1_S2", variant_id="V1", duration_seconds=8),
        ],
        segment_payloads=[
            {"generation_status": "completed", "last_frame_uri": "assets/V1_S1_last.png"},
            {"generation_status": "completed"},
        ],
    )

    assert preflight["status"] == "fail"
    assert "stitch_preflight_failed" in preflight["flags"]
    assert any(check["key"] == "V1_S1.segment_contract" for check in preflight["checks"])


def test_stitch_preflight_blocks_segment_frame_qa_failure():
    runtime = AgentsRuntime()

    preflight = runtime._stitch_preflight(
        segments=[VideoSegmentPlan(segment_id="V1_S1", variant_id="V1", duration_seconds=8)],
        segment_payloads=[
            {
                "segment_id": "V1_S1",
                "generation_status": "completed",
                "segment_contract": {"must_preserve": ["blue harness"]},
                "segment_frame_qa": {
                    "status": "fail",
                    "blocking": True,
                    "flags": ["visual_qa_unusable_frame_sequence"],
                },
            }
        ],
    )
    ledger = runtime._segment_ledger(
        variant_id="V1",
        segment_queue=[{"segment_id": "V1_S1"}],
        segment_payloads=[
            {
                "segment_id": "V1_S1",
                "generation_status": "completed",
                "segment_frame_qa": {
                    "status": "fail",
                    "blocking": True,
                    "flags": ["visual_qa_unusable_frame_sequence"],
                },
            }
        ],
        stitch_preflight=preflight,
    )

    assert preflight["status"] == "fail"
    assert any(check["key"] == "V1_S1.segment_frame_qa" for check in preflight["checks"])
    assert ledger["status"] == "needs_regeneration"
    assert ledger["segments"][0]["segment_frame_qa_status"] == "fail"


def test_segmented_video_generation_uses_local_tail_frame_with_identity_anchors(monkeypatch):
    runtime = AgentsRuntime()
    provider = _FakeCompletedVideoProviderWithoutLastFrameUrl()
    runtime.providers = _FakeRegistry(provider)
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    def fake_extract_last_frame(*, video_path, output_path):
        output_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"))
        return str(output_path)

    def fake_stitch(*, video_paths, output_path):
        output_path.write_bytes(b"stitched-video" * 256)
        return str(output_path)

    monkeypatch.setattr("app.agents.runtime.extract_last_video_frame", fake_extract_last_frame)
    monkeypatch.setattr("app.agents.runtime.stitch_video_files", fake_stitch)
    _patch_valid_segment_frame_sampling(monkeypatch)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="One dress, two moods",
                script="A continuous segmented ad.",
                segments=[
                    {
                        "segment_id": "V1_S1",
                        "variant_id": "V1",
                        "duration_seconds": 12,
                        "first_frame_prompt": "apartment reveal",
                        "last_frame_prompt": "walking out",
                        "motion_prompt": "model smiles in apartment",
                    },
                    {
                        "segment_id": "V1_S2",
                        "variant_id": "V1",
                        "duration_seconds": 12,
                        "first_frame_prompt": "street continuation",
                        "last_frame_prompt": "cta",
                        "motion_prompt": "same model crosses street",
                    },
                ],
            )
        ],
        product_context={"product_name": "silver dress"},
        generation_spec={"size": "9:16", "resolution": "720p", "duration": 24},
    )

    output = runtime.run_video_generation(
        run_id="runtime-video-local-tail",
        script_pack=script_pack,
        storyboard_frames=[{"raw_response": {"image_url": "https://example.com/storyboard-anchor.png"}}],
        creative_specs={"video_size": "9:16", "video_duration_seconds": 24},
        provider="openai",
        model="gpt-4.1",
        runtime_config={"video": {"provider_name": "fake", "model_name": "fake-video"}, "force_regenerate": True},
    )

    assert output.payload["videos"][0]["source"] == "stitched_segments"
    assert provider.requests[0].image_urls == ["https://example.com/storyboard-anchor.png"]
    assert provider.requests[1].image_with_roles == []
    assert provider.requests[1].image_urls[0].startswith("data:image/png;base64,")
    assert provider.requests[1].image_urls[1] == "https://example.com/storyboard-anchor.png"
    assert "image 1 is the previous segment tail frame" in provider.requests[1].prompt
    segment_manifest = output.payload["videos"][0]["segments"][1]["reference_manifest"]
    assert segment_manifest[0]["source"] == "tail_frame"
    assert segment_manifest[0]["provider_usable"] is True
    assert segment_manifest[0]["used"] is True
    assert segment_manifest[0]["transport"] == "data_url"
    assert output.payload["videos"][0]["segment_ledger"]["segments"][1]["hosted_reference_ready"] is True


def test_apimart_segmented_video_skips_unhosted_local_tail_frame(monkeypatch):
    runtime = AgentsRuntime()
    provider = _FakeCompletedVideoProviderWithoutLastFrameUrl()
    runtime.providers = _FakeRegistry(provider)
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    def fake_extract_last_frame(*, video_path, output_path):
        output_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"))
        return str(output_path)

    def fake_stitch(*, video_paths, output_path):
        output_path.write_bytes(b"stitched-video" * 256)
        return str(output_path)

    monkeypatch.setattr("app.agents.runtime.extract_last_video_frame", fake_extract_last_frame)
    monkeypatch.setattr("app.agents.runtime.stitch_video_files", fake_stitch)
    _patch_valid_segment_frame_sampling(monkeypatch)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="One dress, two moods",
                script="A continuous segmented ad.",
                segments=[
                    {"segment_id": "V1_S1", "variant_id": "V1", "duration_seconds": 12, "motion_prompt": "model in apartment"},
                    {"segment_id": "V1_S2", "variant_id": "V1", "duration_seconds": 12, "motion_prompt": "same model in street"},
                ],
            )
        ],
        product_context={"product_name": "silver dress"},
        generation_spec={"size": "9:16", "resolution": "720p", "duration": 24},
    )

    output = runtime.run_video_generation(
        run_id="runtime-video-apimart-no-data-tail",
        script_pack=script_pack,
        storyboard_frames=[{"raw_response": {"image_url": "https://example.com/storyboard-anchor.png"}}],
        creative_specs={"video_size": "9:16", "video_duration_seconds": 24},
        provider="openai",
        model="gpt-4.1",
        runtime_config={
            "video": {"provider_name": "apimart", "model_name": "doubao-seedance-2.0"},
            "force_regenerate": True,
        },
    )

    assert output.payload["videos"][0]["source"] == "stitched_segments"
    assert provider.requests[1].image_urls == ["https://example.com/storyboard-anchor.png"]
    assert provider.requests[1].image_with_roles == []
    assert output.payload["videos"][0]["segments"][1]["reference_mode"] == "anchors"
    segment = output.payload["videos"][0]["segments"][1]
    assert segment["hosted_reference_ready"] is True
    assert any(
        item["source"] == "tail_frame"
        and item["provider_usable"] is False
        and item["used"] is False
        and item["reason"] == "requires_hosted_reference"
        for item in segment["reference_manifest"]
    )
    assert any(
        item["uri"] == "https://example.com/storyboard-anchor.png"
        and item["provider_usable"] is True
        and item["used"] is True
        for item in segment["reference_manifest"]
    )
    ledger_segment = output.payload["videos"][0]["segment_ledger"]["segments"][1]
    assert ledger_segment["hosted_reference_ready"] is True
    assert ledger_segment["reference_manifest"] == segment["reference_manifest"]


def test_segmented_video_generation_truncates_refs_when_provider_accepts_one_image(monkeypatch, tmp_path):
    runtime = AgentsRuntime()
    provider = _FakeCompletedVideoProviderWithoutLastFrameUrl()
    runtime.providers = _FakeRegistry(provider)
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
    anchor = tmp_path / "storyboard_anchor.png"
    anchor.write_bytes(png)

    def fake_extract_last_frame(*, video_path, output_path):
        output_path.write_bytes(png)
        return str(output_path)

    def fake_stitch(*, video_paths, output_path):
        output_path.write_bytes(b"stitched-video" * 256)
        return str(output_path)

    monkeypatch.setattr("app.agents.runtime.extract_last_video_frame", fake_extract_last_frame)
    monkeypatch.setattr("app.agents.runtime.stitch_video_files", fake_stitch)
    _patch_valid_segment_frame_sampling(monkeypatch)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="One dress, two moods",
                script="A continuous segmented ad.",
                segments=[
                    {"segment_id": "V1_S1", "variant_id": "V1", "duration_seconds": 12, "motion_prompt": "apartment"},
                    {"segment_id": "V1_S2", "variant_id": "V1", "duration_seconds": 12, "motion_prompt": "street"},
                ],
            )
        ],
        product_context={"product_name": "silver dress"},
        generation_spec={"size": "9:16", "resolution": "720p", "duration": 24},
    )

    runtime.run_video_generation(
        run_id="runtime-video-reference-board",
        script_pack=script_pack,
        storyboard_frames=[{"image_uri": str(anchor)}],
        creative_specs={"video_size": "9:16", "video_duration_seconds": 24, "max_reference_images": 1},
        provider="openai",
        model="gpt-4.1",
        runtime_config={"video": {"provider_name": "fake", "model_name": "fake-video"}, "force_regenerate": True},
    )

    assert len(provider.requests[1].image_urls) == 1
    assert provider.requests[1].image_urls[0].startswith("data:image/png;base64,")
    assert provider.requests[1].image_with_roles == []
    assert "previous segment tail frame" in provider.requests[1].prompt


def test_segmented_video_generation_preserves_initial_role_references(monkeypatch):
    runtime = AgentsRuntime()
    provider = _FakeCompletedVideoProvider()
    runtime.providers = _FakeRegistry(provider)
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    def fake_extract_last_frame(*, video_path, output_path):
        output_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"))
        return str(output_path)

    def fake_stitch(*, video_paths, output_path):
        output_path.write_bytes(b"stitched-video" * 256)
        return str(output_path)

    monkeypatch.setattr("app.agents.runtime.extract_last_video_frame", fake_extract_last_frame)
    monkeypatch.setattr("app.agents.runtime.stitch_video_files", fake_stitch)
    _patch_valid_segment_frame_sampling(monkeypatch)

    runtime.run_video_generation(
        run_id="runtime-video-role-refs",
        script_pack=VideoScriptPack(
            scripts=[
                VideoScriptItem(
                    variant_id="V1",
                    hook="One dress",
                    script="A continuous segmented ad.",
                    segments=[
                        {"segment_id": "V1_S1", "variant_id": "V1", "duration_seconds": 8, "motion_prompt": "start"},
                        {"segment_id": "V1_S2", "variant_id": "V1", "duration_seconds": 8, "motion_prompt": "continue"},
                    ],
                )
            ],
            product_context={"product_name": "silver dress"},
            generation_spec={"size": "9:16", "resolution": "720p", "duration": 16},
        ),
        creative_specs={
            "video_size": "9:16",
            "video_duration_seconds": 16,
            "image_with_roles": [{"url": "https://example.com/start.png", "role": "first_frame"}],
        },
        provider="openai",
        model="gpt-4.1",
        runtime_config={"video": {"provider_name": "fake", "model_name": "fake-video"}, "force_regenerate": True},
    )

    assert provider.requests[0].image_with_roles == [{"url": "https://example.com/start.png", "role": "first_frame"}]
    assert provider.requests[0].image_urls == []
    assert "role references exactly as temporal anchors" in provider.requests[0].prompt
    assert provider.requests[1].image_with_roles == [{"url": "https://example.com/last-1.png", "role": "first_frame"}]


def test_single_video_generation_truncates_refs_when_provider_accepts_one_image():
    runtime = AgentsRuntime()
    provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(provider)
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)
    png_data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"

    runtime.run_video_generation(
        run_id="runtime-video-single-reference-board",
        script_pack=VideoScriptPack(
            scripts=[
                VideoScriptItem(
                    variant_id="V1",
                    hook="One dress",
                    script="Show one continuous social ad.",
                    shot_list=["show model wearing the dress"],
                )
            ],
            product_context={"product_name": "silver dress"},
            generation_spec={"size": "9:16", "resolution": "720p", "duration": 12},
        ),
        creative_specs={
            "video_size": "9:16",
            "video_duration_seconds": 12,
            "image_urls": [png_data_url, png_data_url],
            "max_reference_images": 1,
        },
        provider="openai",
        model="gpt-4.1",
        runtime_config={"video": {"provider_name": "fake", "model_name": "fake-video"}},
    )

    assert provider.last_request is not None
    assert len(provider.last_request.image_urls) == 1
    assert provider.last_request.image_urls[0].startswith("data:image/png;base64,")


def test_segmented_video_generation_marks_first_segment_submit_failure(monkeypatch):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)
    monkeypatch.setattr(
        runtime,
        "_generate_video_clip_payload",
        lambda **kwargs: (
            {
                "variant_id": kwargs["variant_id"],
                "video_uri": "assets/run/V1_generation_error.txt",
                "source": "placeholder",
                "generation_status": None,
                "error": "payment_required",
            },
            0.0,
            "",
        ),
    )

    output = runtime.run_video_generation(
        run_id="runtime-video-first-segment-failure",
        script_pack=VideoScriptPack(
            scripts=[
                VideoScriptItem(
                    variant_id="V1",
                    hook="Hook",
                    script="Script",
                    segments=[
                        {
                            "segment_id": "V1_S1",
                            "variant_id": "V1",
                            "duration_seconds": 12,
                            "motion_prompt": "first segment",
                        }
                    ],
                )
            ],
            product_context={"product_name": "robe dress"},
            generation_spec={"size": "9:16", "resolution": "720p", "duration": 12},
        ),
        creative_specs={"video_size": "9:16", "video_duration_seconds": 12},
        provider="apimart",
        model="doubao-seedance-2.0",
    )

    video = output.payload["videos"][0]
    assert video["generation_status"] == "failed"
    assert video["source"] == "placeholder"
    assert video["error"] == "payment_required"


def test_storyboard_and_video_generation_do_not_inject_leash_defaults_and_use_structured_specs():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    def fake_generate_image(*, prompt, **kwargs):
        assert "dog leash" not in prompt.lower()
        assert "leash" not in prompt.lower()
        return (
            type(
                "ImageResult",
                (),
                {
                    "estimated_cost": 0.0,
                    "images": [],
                },
            )(),
            "stub-image-provider",
            "stub-image-model",
        )

    fake_provider = _FakeVideoProvider()
    runtime._generate_image = fake_generate_image
    runtime.providers = _FakeRegistry(fake_provider)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Pack faster without messy leaks",
                script="Show the travel toiletry bag holding upright bottles in clear compartments.",
                shot_list=[
                    "Open the bag on a hotel bathroom counter.",
                    "Close-up of upright bottle sleeves and clear compartments.",
                    "Packed bag slides neatly into a carry-on.",
                ],
            )
        ],
        product_context={"product_name": "travel toiletry bag", "audience": "frequent travelers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    storyboard_output = runtime.run_storyboard_image_generation(
        run_id="runtime-storyboard-generic-product",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
    )
    storyboard_prompt = storyboard_output.payload["frames"][0]["prompt"].lower()
    assert "dog leash" not in storyboard_prompt
    assert "travel toiletry bag" in storyboard_prompt

    video_output = runtime.run_video_generation(
        run_id="runtime-video-generic-product",
        script_pack=script_pack,
        creative_specs={
            "video_size": "16:9",
            "resolution": "720p",
            "video_duration_seconds": 5,
            "generate_audio": True,
            "return_last_frame": True,
            "seed": 42,
            "tools": [{"type": "web_search"}],
            "image_urls": ["https://example.com/reference-image.png"],
            "audio_urls": ["https://example.com/reference-audio.wav"],
        },
        provider="apimart",
        model="doubao-seedance-2.0",
        runtime_config={
            "api_base_url": "https://api.apimart.ai/v1/videos/generations",
            "api_key": "dummy",
        },
    )

    video_payload = video_output.payload["videos"][0]
    assert "leash" not in video_payload["prompt"].lower()
    assert "leash_connection_required" not in video_payload.get("quality_constraints", {})
    assert fake_provider.last_request is not None
    assert fake_provider.last_request.generate_audio is True
    assert fake_provider.last_request.return_last_frame is True
    assert fake_provider.last_request.seed == 42
    assert fake_provider.last_request.tools == [{"type": "web_search"}]
    assert fake_provider.last_request.image_urls == ["https://example.com/reference-image.png"]
    assert fake_provider.last_request.audio_urls == ["https://example.com/reference-audio.wav"]


def test_storyboard_uses_video_visual_proof_spec():
    runtime = AgentsRuntime()
    captured_prompts: list[str] = []
    runtime._chat_complete = lambda *args, **kwargs: ("not json", "stub-model", 0.0)
    runtime._local_media_qa = lambda **kwargs: {"status": "pass", "score": 100, "flags": [], "checks": []}

    def fake_generate_image(*, prompt, **kwargs):
        captured_prompts.append(prompt)
        return (
            ImageGenResult(
                model_used="image-model",
                images=[
                    GeneratedImage(
                        b64_json="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
                    )
                ],
            ),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    spec = {
        "proof_mechanism": "front chest D-ring redirects leash tension",
        "semantic_fail_conditions": ["image communicates active pulling instead of controlled anti-pull redirection"],
        "must_show": ["controlled walking posture"],
    }
    output = runtime.run_storyboard_image_generation(
        run_id="runtime-storyboard-visual-proof",
        script_pack=VideoScriptPack(
            scripts=[
                VideoScriptItem(
                    variant_id="V1",
                    hook="Show the front chest D-ring redirecting leash tension",
                    script="Show controlled walking.",
                    shot_list=["Dog walks calmly with visible front chest D-ring."],
                )
            ],
            product_context={"product_name": "anti-pull dog harness", "visual_proof_specs": {"V1": spec}},
            generation_spec={"size": "1:1", "resolution": "720p", "duration": 8},
        ),
        creative_specs={"video_size": "1:1"},
        provider="openai",
        model="gpt-4.1",
        runtime_config={"force_regenerate": True},
    )

    frame = output.payload["frames"][0]
    assert "Visual proof spec" in captured_prompts[0]
    assert "controlled anti-pull redirection" in captured_prompts[0]
    assert frame["visual_proof_spec"] == spec
    assert frame["candidate_frames"][0]["visual_proof_spec"] == spec


def test_storyboard_prompt_uses_director_plan_without_category_lock():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)
    planning = PlanningBrief(
        creative_director_plan={
            "emotional_beats": ["relief"],
            "must_preserve_visuals": ["clear compartments", "upright bottle sleeves"],
            "scene_arc": [
                {"beat": "proof", "scene_direction": "hotel bathroom counter with bottles standing upright"},
            ],
        },
        quality_gates=[{"gate": "category_fit_storyboard"}],
    )
    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="No more suitcase leaks",
                script="Show a compact organizer solving messy travel packing.",
                shot_list=["Open the organizer on a hotel counter"],
            )
        ],
        product_context={"product_name": "travel toiletry bag", "audience": "frequent travelers"},
        generation_spec={"size": "9:16", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_storyboard_image_generation(
        run_id="storyboard-director-generic-product",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
        planning=planning,
    )

    prompt = output.payload["frames"][0]["prompt"].lower()
    assert "hotel bathroom counter" in prompt
    assert "emotional target: relief" in prompt
    assert "clear compartments" in prompt
    assert "do not force fashion/model framing" in prompt


def test_storyboard_frame_exposes_pending_candidate_task(monkeypatch):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    def fake_generate_image(*, prompt, **kwargs):
        return (
            type(
                "ImageResult",
                (),
                {
                    "estimated_cost": 0.0,
                    "images": [],
                    "task_id": "image-task-1",
                    "status": "submitted",
                    "raw_response": {"id": "image-task-1"},
                },
            )(),
            "stub-image-provider",
            "stub-image-model",
        )

    monkeypatch.setattr(runtime, "_generate_image", fake_generate_image)

    output = runtime.run_storyboard_image_generation(
        run_id="storyboard-pending-candidate",
        script_pack=VideoScriptPack(
            scripts=[
                VideoScriptItem(
                    variant_id="V1",
                    hook="Hook",
                    script="Script",
                    shot_list=["Shot 1"],
                )
            ],
            product_context={"product_name": "travel organizer"},
            generation_spec={"size": "9:16", "duration": 5},
        ),
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
    )

    frame = output.payload["frames"][0]
    assert frame["external_task_id"] == "image-task-1"
    assert frame["generation_status"] == "submitted"
    assert frame["candidate_frames"][0]["external_task_id"] == "image-task-1"


def test_storyboard_generation_can_store_multiple_candidates_and_pick_one(monkeypatch):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    candidate_scores = {
        "_cand_1": 61.0,
        "_cand_2": 93.0,
        "_cand_3": 77.0,
    }

    def fake_generate_image(*, prompt, **kwargs):
        image_result = type(
            "ImageResult",
            (),
            {
                "estimated_cost": 0.0,
                "images": [
                    type(
                        "GeneratedImage",
                        (),
                        {
                            "b64_json": base64.b64encode(b"candidate-image-bytes").decode("ascii"),
                            "url": None,
                        },
                    )()
                ],
            },
        )()
        return image_result, "stub-image-provider", "stub-image-model"

    def fake_local_media_qa(*, uri, **kwargs):
        score = next((value for marker, value in candidate_scores.items() if marker in str(uri)), 0.0)
        return {"status": "pass", "score": score, "flags": []}

    monkeypatch.setattr(runtime, "_generate_image", fake_generate_image)
    monkeypatch.setattr(runtime, "_local_media_qa", fake_local_media_qa)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Pack faster without messy leaks",
                script="Show the travel toiletry bag holding upright bottles in clear compartments.",
                shot_list=["Open the bag", "Show bottles upright", "Packshot CTA"],
            )
        ],
        product_context={"product_name": "travel toiletry bag", "audience": "frequent travelers"},
        generation_spec={"size": "9:16", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_storyboard_image_generation(
        run_id="storyboard-candidates",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16", "storyboard_candidate_count": 3},
    )

    frame = output.payload["frames"][0]
    assert frame["selected_candidate_index"] == 1
    assert len(frame["candidate_frames"]) == 3
    assert [candidate["candidate_index"] for candidate in frame["candidate_frames"]] == [0, 1, 2]
    assert frame["candidate_frames"][1]["image_uri"] == frame["image_uri"]
    assert frame["candidate_frames"][1]["visual_qa"]["score"] == frame["visual_qa"]["score"] == 93.0
    assert frame["candidate_frames"][1]["source"] == "b64_json"


def test_video_generation_samples_frames_for_completed_local_videos(tmp_path, monkeypatch):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ('{"video_prompts":[{"variant_id":"V1","prompt":"demo prompt"}]}', "stub-model", 0.0)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Show the result immediately",
                script="Open with the product in use and end on a clean CTA.",
                shot_list=["Open on product close-up", "Demonstrate use", "Finish with CTA"],
            )
        ],
        product_context={"product_name": "travel toiletry bag"},
        generation_spec={"size": "9:16", "resolution": "720p", "duration": 5},
    )

    fake_video_bytes = b"\x00\x00\x00\x20ftypisom" + (b"0" * 2048)

    monkeypatch.setattr(
        runtime,
        "_generate_video_submit_only",
        lambda **kwargs: (
            VideoGenResult(
                model_used="stub-video-model",
                status="completed",
                videos=[
                    GeneratedVideo(
                        b64_data=base64.b64encode(fake_video_bytes).decode("ascii"),
                        task_id="video-task-1",
                        status="completed",
                    )
                ],
            ),
            "stub-video-provider",
            "stub-video-model",
        ),
    )

    def fake_sample_video_frames(*, video_path, output_dir, prefix, count=3):
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for idx in range(count):
            frame = output_dir / f"{prefix}_frame_{idx + 1}.png"
            frame.write_bytes(f"frame-{idx + 1}".encode("ascii"))
            frames.append(str(frame))
        return frames

    monkeypatch.setattr("app.agents.runtime.sample_video_frames", fake_sample_video_frames)

    output = runtime.run_video_generation(
        run_id="runtime-video-frame-sampling",
        script_pack=script_pack,
        creative_specs={"video_size": "9:16", "resolution": "720p", "video_duration_seconds": 5},
        provider="openai",
        model="gpt-4.1",
    )

    video_payload = output.payload["videos"][0]
    assert video_payload["generation_status"] == "completed"
    assert len(video_payload["frame_uris"]) == 3
    assert len(video_payload["generated_video_frames"]) == 3
    assert all(Path(frame["uri"]).exists() for frame in video_payload["generated_video_frames"])
    assert video_payload["frame_uris"] == [frame["uri"] for frame in video_payload["generated_video_frames"]]


def test_attach_generated_video_frames_clears_stale_frame_metadata_when_resampling_returns_none(monkeypatch):
    runtime = AgentsRuntime()
    monkeypatch.setattr(runtime, "_sample_generated_video_frames", lambda **kwargs: ([], []))

    enriched = runtime._attach_generated_video_frames(
        run_id="runtime-video-clear-stale-frames",
        video_payload={
            "variant_id": "V1",
            "video_uri": "assets/runtime-video-clear-stale-frames/V1.mp4",
            "generation_status": "completed",
            "frame_uris": ["assets/old_frame_1.png"],
            "generated_video_frames": [{"uri": "assets/old_frame_1.png"}],
        },
    )

    assert enriched["frame_uris"] == []
    assert enriched["generated_video_frames"] == []


def test_visual_quality_assessment_uses_frame_review_for_completed_videos(tmp_path):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("frame review notes", "stub-model", 0.0)

    video_path = tmp_path / "completed.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x20ftypisom" + (b"1" * 2048))

    variant_set = VariantSet(
        variants=[
            VariantCandidate(
                variant_id="V1",
                angle="show the product immediately",
                hook="The first second should explain the product",
                message="Lead with the product and keep continuity clean.",
            )
        ]
    )

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-video-frame-review",
        variant_set=variant_set,
        videos={
            "videos": [
                {
                    "variant_id": "V1",
                    "video_uri": str(video_path),
                    "uri": str(video_path),
                    "generation_status": "completed",
                    "source": "local_file",
                }
            ]
        },
        video_scripts={
            "scripts": [
                {
                    "variant_id": "V1",
                    "hook": "The first second should explain the product",
                    "shot_plan": [
                        {
                            "shot_id": "shot-1",
                            "intent": "thumb_stop",
                            "duration_seconds": 1.5,
                            "product_continuity_constraints": ["same blue bottle", "same cap shape"],
                        }
                    ],
                }
            ]
        },
        social_review_contract={
            "review_profile": "social_video",
            "required_checks": ["first_frame_clarity", "continuity"],
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    report = output.payload["reports"][0]
    asset_report = report["asset_reports"][0]

    assert summary["recommended_action"] == "manual_review"
    assert summary["qa_status"] == "warn"
    assert "visual_qa_needs_frame_review" in asset_report["flags"]
    assert any(check["status"] == "manual_review" for check in asset_report["checks"])


def test_visual_quality_assessment_surfaces_product_truth_flags():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("product truth review notes", "stub-model", 0.0)

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-product-truth-flags",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show the harness clearly.",
                )
            ]
        ),
        intake={
            "product_name": "blue pet harness",
            "product_truth_contract": {
                "colors": ["blue", "black"],
                "must_preserve": ["blue pet harness"],
            },
        },
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": "assets/nonexistent.png",
                    "visual_qa": {
                        "status": "warn",
                        "score": 72,
                        "flags": ["visual_qa_product_truth_structure_review"],
                        "checks": [{"key": "product_truth_structure", "status": "manual_review"}],
                    },
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    assert summary["recommended_action"] == "manual_review"
    assert summary["product_truth_flags"] == ["visual_qa_product_truth_structure_review"]
    assert "visual_qa_product_truth_structure_review" in summary["issues"]


def test_visual_quality_assessment_surfaces_visual_proof_spec():
    runtime = AgentsRuntime()
    captured_prompt: dict[str, str] = {}

    def fake_chat_complete(provider, model, prompt, runtime_config, **kwargs):
        captured_prompt["prompt"] = prompt
        return "visual proof review notes", "stub-model", 0.0

    runtime._chat_complete = fake_chat_complete

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-visual-proof-spec",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="anti-pull control",
                    hook="Show the front chest D-ring redirecting leash tension",
                    message="Do not show uncontrolled pulling.",
                )
            ]
        ),
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": "assets/nonexistent.png",
                    "visual_qa": {"status": "pass", "score": 98, "flags": [], "checks": []},
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    report = output.payload["reports"][0]
    spec = summary["visual_proof_spec"]
    assert "semantic_fail_conditions" in spec
    assert report["asset_reports"][0]["visual_proof_spec"] == spec
    assert "visual_proof_spec" in captured_prompt["prompt"]
    assert "only for visual quality and product truth" in captured_prompt["prompt"]
    assert "Do not make claim-safety, policy, legal, or platform-compliance decisions" in captured_prompt["prompt"]


def test_visual_quality_assessment_compresses_local_images_for_model_review(tmp_path):
    from PIL import Image

    image_path = tmp_path / "large-review-source.png"
    Image.new("RGB", (1800, 1400), color=(40, 120, 210)).save(image_path)
    raw_data_url_length = len(AgentsRuntime()._local_image_to_data_url(str(image_path)) or "")

    runtime = AgentsRuntime()
    captured: dict[str, object] = {}

    def fake_chat_complete(provider, model, prompt, runtime_config, **kwargs):
        captured["image_urls"] = list(kwargs.get("image_urls") or [])
        captured["runtime_config"] = runtime_config
        return (
            '{"model_summary":"ok","visual_proof_reviews":[{"variant_id":"V1","status":"pass","evidence":"product visible","failed_conditions":[]}]}',
            "stub-model",
            0.0,
        )

    runtime._chat_complete = fake_chat_complete

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-visual-review-compressed-input",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show a clear product image.",
                )
            ]
        ),
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": str(image_path),
                    "visual_qa": {"status": "pass", "score": 98, "flags": [], "checks": []},
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    image_urls = captured["image_urls"]
    assert isinstance(image_urls, list)
    assert image_urls[0].startswith("data:image/jpeg;base64,")
    assert len(image_urls[0]) < raw_data_url_length
    runtime_config = captured["runtime_config"]
    assert isinstance(runtime_config, dict)
    assert runtime_config["extra"]["request_timeout_seconds"] == 180
    manifest = output.payload["model_media_inputs"]["manifest"][0]
    assert manifest["review_transport"] == "compressed_data_url"
    assert manifest["review_input_chars"] == len(image_urls[0])


def test_visual_quality_assessment_blocks_failed_visual_proof_review():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: (
        """
        {
          "model_summary": "The image shows uncontrolled pulling.",
          "visual_proof_reviews": [
            {
              "variant_id": "V1",
              "status": "fail",
              "visual_score": 42,
              "evidence": "Dog is lunging forward with taut leash.",
              "failed_conditions": ["image communicates active pulling instead of controlled anti-pull redirection"]
            }
          ]
        }
        """,
        "stub-model",
        0.0,
    )

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-visual-proof-fail",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="anti-pull control",
                    hook="Show the front chest D-ring redirecting leash tension",
                    message="Do not show uncontrolled pulling.",
                )
            ]
        ),
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": "assets/nonexistent.png",
                    "visual_qa": {"status": "pass", "score": 98, "flags": [], "checks": []},
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    report = output.payload["reports"][0]
    assert output.payload["model_summary"] == "The image shows uncontrolled pulling."
    assert summary["qa_status"] == "fail"
    assert summary["recommended_action"] == "request_regeneration"
    assert "visual_qa_visual_proof_failed" in summary["issues"]
    assert report["visual_proof_qa"]["evidence"] == "Dog is lunging forward with taut leash."


def test_visual_quality_assessment_applies_model_visual_score():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: (
        """
        {
          "model_summary": "The image is plausible but weak.",
          "visual_proof_reviews": [
            {
              "variant_id": "V1",
              "status": "warn",
              "visual_score": 62,
              "evidence": "Product is visible but the benefit is not strongly proven.",
              "failed_conditions": []
            }
          ]
        }
        """,
        "stub-model",
        0.0,
    )

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-visual-score-applied",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="comfort",
                    hook="Show comfort clearly",
                    message="Product visible.",
                )
            ]
        ),
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": "assets/nonexistent.png",
                    "visual_qa": {"status": "pass", "score": 95, "flags": [], "checks": []},
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    report = output.payload["reports"][0]
    assert output.payload["model_summary"] == "The image is plausible but weak."
    assert summary["visual_score"] == 62.0
    assert report["visual_score"] == 62.0


def test_visual_quality_assessment_marks_unavailable_model_review_for_manual_review():
    runtime = AgentsRuntime()

    def fail_chat(*args, **kwargs):
        raise RuntimeError("provider timeout")

    runtime._chat_complete = fail_chat

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-visual-model-unavailable",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show a clear product image.",
                )
            ]
        ),
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": "assets/nonexistent.png",
                    "visual_qa": {"status": "pass", "score": 98, "flags": [], "checks": []},
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    report = output.payload["reports"][0]
    assert output.payload["model_review_status"] == "unavailable"
    assert output.payload["model_summary"].startswith("model_review_unavailable:")
    assert summary["qa_status"] == "warn"
    assert summary["recommended_action"] == "manual_review"
    assert summary["visual_score"] is None
    assert "visual_qa_model_review_unavailable" in summary["issues"]
    assert report["model_review_status"] == "unavailable"
    assert report["visual_score"] is None


def test_visual_quality_assessment_rejects_model_review_without_scores():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: (
        """
        {
          "model_summary": "The image looks acceptable.",
          "visual_proof_reviews": [
            {
              "variant_id": "V1",
              "status": "pass",
              "evidence": "Product is visible.",
              "failed_conditions": []
            }
          ]
        }
        """,
        "stub-model",
        0.0,
    )

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-visual-model-missing-score",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show a clear product image.",
                )
            ]
        ),
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": "assets/nonexistent.png",
                    "visual_qa": {"status": "pass", "score": 95, "flags": [], "checks": []},
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    report = output.payload["reports"][0]
    assert output.payload["model_review_status"] == "unavailable"
    assert output.payload["model_summary"].startswith("model_review_unavailable: parse_error:")
    assert summary["qa_status"] == "warn"
    assert summary["recommended_action"] == "manual_review"
    assert summary["visual_score"] is None
    assert "visual_qa_model_review_unavailable" in summary["issues"]
    assert report["model_review_status"] == "unavailable"
    assert report["visual_score"] is None


def test_visual_quality_assessment_blocks_image_asset_contract_failure():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("image contract review notes", "stub-model", 0.0)

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-image-contract-fail",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show the harness clearly.",
                )
            ]
        ),
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": "assets/nonexistent.png",
                    "visual_qa": {"status": "pass", "score": 98, "flags": [], "checks": []},
                    "image_asset_contract": {
                        "status": "fail",
                        "blocking": True,
                        "flags": ["visual_qa_placeholder"],
                    },
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    report = output.payload["reports"][0]
    assert summary["qa_status"] == "fail"
    assert summary["recommended_action"] == "request_regeneration"
    assert "visual_qa_placeholder" in summary["issues"]
    assert report["asset_reports"][0]["image_asset_contract"]["blocking"] is True


def test_visual_quality_assessment_flags_human_anatomy_review_for_model_videos(tmp_path):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("human review notes", "stub-model", 0.0)

    video_path = tmp_path / "model.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x20ftypisom" + (b"1" * 2048))

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-video-human-review",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="model styling",
                    hook="A model wearing the dress walks slowly",
                    message="Keep the same model and dress visible.",
                )
            ]
        ),
        videos={
            "videos": [
                {
                    "variant_id": "V1",
                    "video_uri": str(video_path),
                    "uri": str(video_path),
                    "generation_status": "completed",
                    "source": "local_file",
                    "prompt": "A real model wearing the silver dress.",
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    asset_report = output.payload["reports"][0]["asset_reports"][0]
    assert summary["qa_status"] == "warn"
    assert summary["recommended_action"] == "manual_review"
    assert "visual_qa_human_anatomy_review" in asset_report["flags"]


def test_visual_quality_assessment_treats_unusable_extracted_frames_as_manual_review(tmp_path):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("frame review notes", "stub-model", 0.0)

    video_path = tmp_path / "completed.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x20ftypisom" + (b"1" * 2048))

    frame_paths = []
    for idx in range(3):
        frame_path = tmp_path / f"placeholder_{idx + 1}.png"
        frame_path.write_bytes(b"")
        frame_paths.append(str(frame_path))

    variant_set = VariantSet(
        variants=[
            VariantCandidate(
                variant_id="V1",
                angle="clear product intro",
                hook="Lead with the product immediately",
                message="Open strong and keep the edit truthful.",
            )
        ]
    )

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-video-unusable-frames",
        variant_set=variant_set,
        videos={
            "videos": [
                {
                    "variant_id": "V1",
                    "video_uri": str(video_path),
                    "uri": str(video_path),
                    "generation_status": "completed",
                    "source": "local_file",
                    "frame_uris": frame_paths,
                }
            ]
        },
        social_review_contract={"review_profile": "social_video", "required_checks": []},
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    asset_report = output.payload["reports"][0]["asset_reports"][0]

    assert summary["recommended_action"] == "manual_review"
    assert summary["qa_status"] == "warn"
    assert "visual_qa_unusable_frame_sequence" in asset_report["flags"]
    assert any(check["key"] == "frame_sequence_quality" and check["status"] == "manual_review" for check in asset_report["checks"])


def test_visual_quality_assessment_surfaces_required_frame_checks_without_shot_plan(tmp_path):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("frame review notes", "stub-model", 0.0)

    video_path = tmp_path / "completed.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x20ftypisom" + (b"1" * 2048))

    frame_paths = []
    for idx in range(3):
        frame_path = tmp_path / f"usable_{idx + 1}.png"
        frame_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mP8z/CfAQgwgImBASwAAB0JAgm1nxsAAAAASUVORK5CYII="))
        frame_paths.append(str(frame_path))

    variant_set = VariantSet(
        variants=[
            VariantCandidate(
                variant_id="V1",
                angle="truthful demo",
                hook="Show what the buyer gets",
                message="Keep continuity and CTA readable.",
            )
        ]
    )

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-video-required-checks",
        variant_set=variant_set,
        videos={
            "videos": [
                {
                    "variant_id": "V1",
                    "video_uri": str(video_path),
                    "uri": str(video_path),
                    "generation_status": "completed",
                    "source": "local_file",
                    "frame_uris": frame_paths,
                }
            ]
        },
        social_review_contract={
            "review_profile": "social_video",
            "required_checks": ["continuity", "product_truth", "cta_clarity"],
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    asset_report = output.payload["reports"][0]["asset_reports"][0]

    assert summary["recommended_action"] == "manual_review"
    assert summary["qa_status"] == "warn"
    assert "visual_qa_continuity_frame_check" in asset_report["flags"]
    assert "visual_qa_product_truth_frame_check" in asset_report["flags"]
    assert "visual_qa_cta_clarity_frame_check" in asset_report["flags"]
    assert any(check["key"] == "continuity" and check["status"] == "manual_review" for check in asset_report["checks"])
    assert any(check["key"] == "product_truth" and check["status"] == "manual_review" for check in asset_report["checks"])
    assert any(check["key"] == "cta_clarity" and check["status"] == "manual_review" for check in asset_report["checks"])


def test_visual_quality_assessment_requests_regeneration_on_stitch_preflight_failure(tmp_path):
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("stitch preflight notes", "stub-model", 0.0)

    video_path = tmp_path / "segment_pending.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x20ftypisom" + (b"1" * 2048))

    output = runtime.run_visual_quality_assessment(
        run_id="runtime-stitch-preflight-visual-qa",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="multi-segment proof",
                    hook="Keep the same product moving",
                    message="Segmented video should stitch cleanly.",
                )
            ]
        ),
        videos={
            "videos": [
                {
                    "variant_id": "V1",
                    "video_uri": str(video_path),
                    "uri": str(video_path),
                    "generation_status": "completed",
                    "source": "segmented_pending",
                    "stitch_preflight": {
                        "status": "fail",
                        "flags": ["stitch_preflight_failed"],
                        "checks": [{"key": "V1_S1.tail_frame", "status": "fail"}],
                    },
                }
            ]
        },
        provider="openai",
        model="gpt-4.1",
    )

    summary = output.payload["variant_summaries"][0]
    asset_report = output.payload["reports"][0]["asset_reports"][0]

    assert summary["qa_status"] == "fail"
    assert summary["recommended_action"] == "request_regeneration"
    assert "stitch_preflight_failed" in summary["issues"]
    assert "stitch_preflight_failed" in asset_report["flags"]
    assert asset_report["stitch_preflight"]["status"] == "fail"


# ---------------------------------------------------------------------------
# _parse_llm_json tests
# ---------------------------------------------------------------------------

def test_parse_llm_json_valid_raw():
    runtime = AgentsRuntime()
    result = runtime._parse_llm_json('{"scripts": [1, 2, 3]}', "scripts")
    assert result == {"scripts": [1, 2, 3]}


def test_parse_llm_json_valid_with_fence():
    runtime = AgentsRuntime()
    result = runtime._parse_llm_json(
        '```json\n{"frames": [{"id": 1}]}\n```',
        "frames",
    )
    assert result == {"frames": [{"id": 1}]}


def test_parse_llm_json_valid_with_plain_fence():
    runtime = AgentsRuntime()
    result = runtime._parse_llm_json(
        '```\n{"video_prompts": ["prompt1"]}\n```',
        "video_prompts",
    )
    assert result == {"video_prompts": ["prompt1"]}


def test_parse_llm_json_strips_surrounding_whitespace():
    runtime = AgentsRuntime()
    result = runtime._parse_llm_json(
        '  \n{"key": "value"}\n  ',
        "key",
    )
    assert result == {"key": "value"}


def test_parse_llm_json_returns_full_dict_not_just_value():
    runtime = AgentsRuntime()
    result = runtime._parse_llm_json(
        '{"scripts": [1, 2], "meta": {"model": "gpt-4"}}',
        "scripts",
    )
    assert result == {"scripts": [1, 2], "meta": {"model": "gpt-4"}}


def test_parse_llm_json_empty_string_raises():
    runtime = AgentsRuntime()
    with pytest.raises(ValueError, match="empty"):
        runtime._parse_llm_json("", "scripts")


def test_parse_llm_json_whitespace_only_raises():
    runtime = AgentsRuntime()
    with pytest.raises(ValueError, match="empty"):
        runtime._parse_llm_json("   \n  \t  ", "scripts")


def test_parse_llm_json_invalid_json_raises():
    runtime = AgentsRuntime()
    with pytest.raises(ValueError, match="Failed to parse"):
        runtime._parse_llm_json("not valid json", "scripts")


def test_parse_llm_json_missing_schema_key_raises():
    runtime = AgentsRuntime()
    with pytest.raises(ValueError, match="not found"):
        runtime._parse_llm_json('{"other_key": 1}', "scripts")


def test_parse_llm_json_array_response_raises():
    runtime = AgentsRuntime()
    with pytest.raises(ValueError, match="not a JSON object"):
        runtime._parse_llm_json("[1, 2, 3]", "scripts")


def test_parse_llm_json_non_string_input_raises():
    runtime = AgentsRuntime()
    with pytest.raises(ValueError, match="empty"):
        runtime._parse_llm_json(None, "scripts")  # type: ignore[arg-type]


def test_chat_complete_stage_extra_overrides_agent_defaults():
    captured: dict[str, object] = {}

    class CapturingProvider:
        def chat_complete(self, request, *, api_base_url=None, api_key=None, extra=None):
            captured["extra"] = dict(extra or {})
            return type("Response", (), {"text": "ok", "model_used": request.model, "estimated_cost": 0.0})()

    class CapturingRegistry:
        def get(self, provider):
            return CapturingProvider()

    runtime = AgentsRuntime()
    runtime.providers = CapturingRegistry()
    runtime._chat_complete(
        "kimi",
        "kimi-k2.6",
        "ping",
        {
            "thinking_mode": "auto",
            "max_output_tokens": 1800,
            "request_timeout_seconds": 90,
            "extra": {
                "thinking_mode": "disabled",
                "max_output_tokens": 2400,
                "request_timeout_seconds": 180,
            },
        },
    )

    assert captured["extra"] == {
        "thinking_mode": "disabled",
        "max_output_tokens": 2400,
        "request_timeout_seconds": 180,
    }


def test_visual_quality_assessment_disables_kimi_thinking_for_model_review():
    runtime = AgentsRuntime()
    captured: dict[str, object] = {}

    def fake_chat_complete(provider, model, prompt, runtime_config, **kwargs):
        captured["provider"] = provider
        captured["model"] = model
        captured["runtime_config"] = runtime_config
        return (
            '{"model_summary":"ok","visual_proof_reviews":[{"variant_id":"V1","status":"pass","visual_score":84,"evidence":"product visible","failed_conditions":[]}]}',
            "stub-model",
            0.0,
        )

    runtime._chat_complete = fake_chat_complete
    runtime.run_visual_quality_assessment(
        run_id="runtime-visual-kimi-review-config",
        variant_set=VariantSet(
            variants=[
                VariantCandidate(
                    variant_id="V1",
                    angle="secure fit",
                    hook="Clip in confidence",
                    message="Show a clear product image.",
                )
            ]
        ),
        copy_images={
            "image_assets": [
                {
                    "variant_id": "V1",
                    "uri": "assets/nonexistent.png",
                    "visual_qa": {"status": "pass", "score": 95, "flags": [], "checks": []},
                }
            ]
        },
        provider="kimi",
        model="kimi-k2.6",
        runtime_config={
            "thinking_mode": "auto",
            "max_output_tokens": 1800,
            "request_timeout_seconds": 90,
            "extra": {},
        },
    )

    runtime_config = captured["runtime_config"]
    assert isinstance(runtime_config, dict)
    assert runtime_config["thinking_mode"] == "disabled"
    assert runtime_config["max_output_tokens"] == 2400
    assert runtime_config["request_timeout_seconds"] == 180
    assert runtime_config["extra"]["thinking_mode"] == "disabled"
    assert runtime_config["extra"]["max_output_tokens"] == 2400
    assert runtime_config["extra"]["request_timeout_seconds"] == 180


def test_evaluation_selection_sends_compressed_images_to_kimi(tmp_path):
    image_path = tmp_path / "variant.png"
    image_path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    ))
    captured = {}

    def fake_chat_complete(provider, model, prompt, runtime_config, **kwargs):
        captured["provider"] = provider
        captured["model"] = model
        captured["prompt"] = prompt
        captured["runtime_config"] = runtime_config
        captured["image_urls"] = kwargs.get("image_urls")
        return (
            '{"variants":[{"variant_id":"V1","hook_appeal":80,"copy_clarity":79,'
            '"brand_alignment":78,"visual_execution":77,"compliance_safety":90,'
            '"total_score":81,"compliance_level":"low","recommended_action":"approve_variant",'
            '"compliance_block":{"level":"low","score":90,"risks":[],"reasons":["No prohibited claim."],"recommendation":"approve_variant"},'
            '"brief_reason":"Best image."}]}',
            "kimi-k2.6",
            0.0,
        )

    runtime = AgentsRuntime()
    runtime._chat_complete = fake_chat_complete
    runtime_config = {"extra": {}}

    output = runtime.run_evaluation_selection(
        run_id="eval-image-test",
        variant_set=VariantSet(variants=[
            VariantCandidate(variant_id="V1", angle="comfort", hook="Walk calmer", message="Padded harness.")
        ]),
        copy_bundle=CopyImageBundle(
            copy_variants=[
                CopyVariant(
                    variant_id="V1",
                    primary_text="Walk calmer with padded support.",
                    headline="Calmer Walks",
                    description="Padded harness.",
                    call_to_action="Shop Now",
                )
            ],
            image_assets=[
                ImageAssetRef(variant_id="V1", uri=str(image_path), prompt="dog harness ad")
            ],
        ),
        script_pack=VideoScriptPack(scripts=[]),
        video_bundle=VideoBundle(videos=[]),
        visual_quality={"variant_summaries": [{"variant_id": "V1", "qa_status": "pass", "visual_score": 88}]},
        provider="kimi",
        model="kimi-k2.6",
        runtime_config=runtime_config,
    )

    assert captured["image_urls"][0].startswith("data:image/jpeg;base64,")
    assert captured["runtime_config"]["extra"]["request_timeout_seconds"] == 180
    assert captured["runtime_config"]["extra"]["thinking_mode"] == "disabled"
    assert captured["runtime_config"]["extra"]["max_output_tokens"] == 6000
    assert "Attached images map to variants" in captured["prompt"]
    assert "Include a compliance_block object per variant" in captured["prompt"]
    assert output.payload["model_media_inputs"]["image_count"] == 1
    assert output.payload["model_media_inputs"]["manifest"][0]["review_transport"] == "compressed_data_url"
    ranked = output.payload["evaluation_result"]["ranked_variants"][0]
    assert ranked["sub_scores"]["compliance_safety"] == 90
    assert ranked["compliance_block"]["reasons"] == ["No prohibited claim."]
    assert output.payload["compliance_block"]["V1"]["score"] == 90
    assert runtime_config == {"extra": {}}


def test_evaluation_selection_forces_kimi_review_budget(tmp_path):
    image_path = tmp_path / "variant.png"
    image_path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    ) * 40)
    captured = {}

    def fake_chat_complete(provider, model, prompt, runtime_config, **kwargs):
        captured["runtime_config"] = runtime_config
        return (
            '{"variants":[{"variant_id":"V1","hook_appeal":80,"copy_clarity":79,'
            '"brand_alignment":78,"visual_execution":77,"compliance_safety":90,'
            '"total_score":81,"compliance_level":"low","recommended_action":"approve_variant",'
            '"compliance_block":{"level":"low","score":90,"risks":[],"reasons":["No prohibited claim."],"recommendation":"approve_variant"},'
            '"brief_reason":"Best image."}]}',
            "kimi-k2.6",
            0.0,
        )

    runtime = AgentsRuntime()
    runtime._chat_complete = fake_chat_complete
    runtime.run_evaluation_selection(
        run_id="eval-kimi-budget",
        variant_set=VariantSet(variants=[
            VariantCandidate(variant_id="V1", angle="comfort", hook="Walk calmer", message="Padded harness.")
        ]),
        copy_bundle=CopyImageBundle(
            copy_variants=[
                CopyVariant(
                    variant_id="V1",
                    primary_text="Walk calmer with padded support.",
                    headline="Calmer Walks",
                    description="Padded harness.",
                    call_to_action="Shop Now",
                )
            ],
            image_assets=[ImageAssetRef(variant_id="V1", uri=str(image_path), prompt="dog harness ad")],
        ),
        script_pack=VideoScriptPack(scripts=[]),
        video_bundle=VideoBundle(videos=[]),
        visual_quality={"variant_summaries": [{"variant_id": "V1", "qa_status": "pass", "visual_score": 88}]},
        provider="kimi",
        model="kimi-k2.6",
        runtime_config={
            "thinking_mode": "auto",
            "max_output_tokens": 1800,
            "request_timeout_seconds": 90,
            "extra": {},
        },
    )

    runtime_config = captured["runtime_config"]
    assert runtime_config["thinking_mode"] == "disabled"
    assert runtime_config["max_output_tokens"] == 6000
    assert runtime_config["request_timeout_seconds"] == 180
    assert runtime_config["extra"]["thinking_mode"] == "disabled"
    assert runtime_config["extra"]["max_output_tokens"] == 6000
    assert runtime_config["extra"]["request_timeout_seconds"] == 180


def test_evaluation_selection_rejects_unscored_model_response(tmp_path):
    image_path = tmp_path / "variant.png"
    image_path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    ))

    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: (
        '{"variants":[{"variant_id":"V1","brief_reason":"looks fine"}]}',
        "kimi-k2.6",
        0.0,
    )

    with pytest.raises(ValueError, match="missing total_score"):
        runtime.run_evaluation_selection(
            run_id="eval-unscored-response",
            variant_set=VariantSet(variants=[
                VariantCandidate(variant_id="V1", angle="comfort", hook="Walk calmer", message="Padded harness.")
            ]),
            copy_bundle=CopyImageBundle(
                copy_variants=[
                    CopyVariant(
                        variant_id="V1",
                        primary_text="Walk calmer with padded support.",
                        headline="Calmer Walks",
                        description="Padded harness.",
                        call_to_action="Shop Now",
                    )
                ],
                image_assets=[
                    ImageAssetRef(variant_id="V1", uri=str(image_path), prompt="dog harness ad")
                ],
            ),
            script_pack=VideoScriptPack(scripts=[]),
            video_bundle=VideoBundle(videos=[]),
            visual_quality={"variant_summaries": [{"variant_id": "V1", "qa_status": "pass", "visual_score": 88}]},
            provider="kimi",
            model="kimi-k2.6",
        )


def test_evaluation_selection_gates_unavailable_visual_model_review(tmp_path):
    image_path = tmp_path / "variant.png"
    image_path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    ) * 40)

    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: (
        '{"variants":[{"variant_id":"V1","hook_appeal":86,"copy_clarity":84,'
        '"brand_alignment":82,"visual_execution":80,"compliance_safety":90,'
        '"total_score":88,"compliance_level":"low","recommended_action":"approve_variant",'
        '"compliance_block":{"level":"low","score":90,"risks":[],"reasons":["No prohibited claim."],"recommendation":"approve_variant"},'
        '"brief_reason":"Strong candidate."}]}',
        "kimi-k2.6",
        0.0,
    )

    output = runtime.run_evaluation_selection(
        run_id="eval-visual-model-unavailable",
        variant_set=VariantSet(variants=[
            VariantCandidate(variant_id="V1", angle="comfort", hook="Walk calmer", message="Padded harness.")
        ]),
        copy_bundle=CopyImageBundle(
            copy_variants=[
                CopyVariant(
                    variant_id="V1",
                    primary_text="Walk calmer with padded support.",
                    headline="Calmer Walks",
                    description="Padded harness.",
                    call_to_action="Shop Now",
                )
            ],
            image_assets=[ImageAssetRef(variant_id="V1", uri=str(image_path), prompt="dog harness ad")],
        ),
        script_pack=VideoScriptPack(scripts=[]),
        video_bundle=VideoBundle(videos=[]),
        visual_quality={
            "variant_summaries": [
                {
                    "variant_id": "V1",
                    "qa_status": "warn",
                    "visual_score": None,
                    "recommended_action": "manual_review",
                    "issues": ["visual_qa_model_review_unavailable"],
                }
            ]
        },
        provider="kimi",
        model="kimi-k2.6",
    )

    ranked = output.payload["evaluation_result"]["ranked_variants"][0]
    assert ranked["total_score"] == 59.0
    assert ranked["recommended_action"] == "manual_review"
    assert ranked["sub_scores"]["visual_qa"] == 50.0
    assert "Visual QA model review unavailable." in ranked["reasons"]


# ---------------------------------------------------------------------------
# run_video_scripting LLM integration tests
# ---------------------------------------------------------------------------


def test_video_scripting_llm_success_path_uses_parsed_json():
    """When LLM returns valid JSON, scripts come from the parsed output, not the template."""
    runtime = AgentsRuntime()

    llm_output = (
        '{"scripts": ['
        '{"variant_id": "V1", "hook": "LLM hook text", "script": "LLM script body", "shot_list": ["LLM shot 1", "LLM shot 2"]},'
        '{"variant_id": "V2", "hook": "Second hook", "script": "Second script", "shot_list": ["Shot A"]}'
        "]}"
    )
    runtime._chat_complete = lambda *args, **kwargs: (llm_output, "gpt-4.1", 0.05)

    variant_set = VariantSet(
        variants=[
            VariantCandidate(
                variant_id="V1",
                angle="template angle",
                hook="template hook",
                message="template message",
            )
        ]
    )
    intake = ProductIntake(
        product_name="test product",
        business_context={},
        asset_media_summary="Test summary.",
    )

    output = runtime.run_video_scripting(
        run_id="test-llm-success",
        variant_set=variant_set,
        intake=intake,
        business_context={
            "target_audience": "test audience",
            "key_value_props": ["test prop"],
            "primary_cta": "Buy Now",
        },
        provider="openai",
        model="gpt-4.1",
        creative_specs={},
        pipeline_mode=None,
    )

    assert output.model_used == "gpt-4.1"
    assert ":fallback_to_template" not in output.model_used
    scripts = output.payload["scripts"]
    assert len(scripts) == 2
    assert scripts[0]["variant_id"] == "V1"
    assert scripts[0]["hook"] == "LLM hook text"
    assert scripts[0]["script"] == "LLM script body"
    assert scripts[0]["shot_list"] == ["LLM shot 1", "LLM shot 2"]
    assert scripts[1]["variant_id"] == "V2"
    assert scripts[1]["hook"] == "Second hook"
    # Template-specific phrasing MUST NOT appear in LLM success path
    script_text_combined = " ".join(
        [scripts[0]["hook"], scripts[0]["script"], *scripts[0]["shot_list"]]
    ).lower()
    assert "variant hook:" not in script_text_combined
    assert "variant message:" not in script_text_combined


def test_video_scripting_llm_parse_failure_falls_back_to_template():
    """When LLM JSON is unparseable, fall back to template loop and tag model_used."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("not valid json", "gpt-4.1", 0.05)

    variant_set = VariantSet(
        variants=[
            VariantCandidate(
                variant_id="V1",
                angle="organized packing",
                hook="Pack faster",
                message="Keeps bottles upright.",
            )
        ]
    )
    intake = ProductIntake(
        product_name="travel toiletry bag",
        business_context={},
        asset_media_summary="Compact zip organizer.",
    )

    output = runtime.run_video_scripting(
        run_id="test-llm-fallback",
        variant_set=variant_set,
        intake=intake,
        business_context={
            "target_audience": "travelers",
            "key_value_props": ["keeps bottles upright"],
            "primary_cta": "Shop Now",
        },
        provider="openai",
        model="gpt-4.1",
        creative_specs={},
        pipeline_mode=None,
    )

    assert output.model_used == "gpt-4.1:fallback_to_template"
    scripts = output.payload["scripts"]
    assert len(scripts) == 1
    # Template content markers should appear
    script_text = " ".join(
        [scripts[0]["hook"], scripts[0]["script"], *scripts[0]["shot_list"]]
    ).lower()
    assert "variant hook:" in script_text or "variant message:" in script_text
    assert "travel toiletry bag" in script_text


def test_video_scripting_llm_empty_response_falls_back():
    """Empty LLM response triggers ValueError from _parse_llm_json and falls back."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("", "gpt-4.1", 0.05)

    variant_set = VariantSet(
        variants=[
            VariantCandidate(variant_id="V1", angle="test angle", hook="hook text", message="msg text")
        ]
    )
    intake = ProductIntake(
        product_name="widget",
        business_context={},
        asset_media_summary="A widget.",
    )

    output = runtime.run_video_scripting(
        run_id="test-empty-response",
        variant_set=variant_set,
        intake=intake,
        business_context={
            "target_audience": "buyers",
            "key_value_props": ["durable"],
            "primary_cta": "Buy",
        },
        provider="openai",
        model="gpt-4.1",
        creative_specs={},
        pipeline_mode=None,
    )

    assert ":fallback_to_template" in output.model_used
    assert len(output.payload["scripts"]) == 1
    assert output.payload["scripts"][0]["variant_id"] == "V1"


def test_video_scripting_llm_missing_scripts_key_falls_back():
    """LLM JSON without 'scripts' key falls back to template."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ('{"other_key": [1,2,3]}', "gpt-4.1", 0.05)

    variant_set = VariantSet(
        variants=[
            VariantCandidate(variant_id="V1", angle="test angle", hook="hook text", message="msg text")
        ]
    )
    intake = ProductIntake(
        product_name="widget",
        business_context={},
        asset_media_summary="A widget.",
    )

    output = runtime.run_video_scripting(
        run_id="test-missing-key",
        variant_set=variant_set,
        intake=intake,
        business_context={
            "target_audience": "buyers",
            "key_value_props": ["durable"],
            "primary_cta": "Buy",
        },
        provider="openai",
        model="gpt-4.1",
        creative_specs={},
        pipeline_mode=None,
    )

    assert ":fallback_to_template" in output.model_used
    assert len(output.payload["scripts"]) == 1
    assert output.payload["scripts"][0]["variant_id"] == "V1"


def test_video_scripting_llm_success_path_populates_tiktok_payload():
    """When pipeline_mode is tiktok_shop_video and LLM succeeds, tiktok field must be populated."""
    runtime = AgentsRuntime()

    llm_output = (
        '{"scripts": ['
        '{"variant_id": "V1", "hook": "TikTok hook text", "script": "TikTok script body", "shot_list": ["TikTok shot 1", "TikTok shot 2"]}'
        "]}"
    )
    runtime._chat_complete = lambda *args, **kwargs: (llm_output, "gpt-4.1", 0.05)

    variant_set = VariantSet(
        variants=[
            VariantCandidate(
                variant_id="V1",
                angle="organized packing",
                hook="Pack faster without messy leaks",
                message="Keeps travel-size bottles upright and separated.",
            )
        ]
    )
    intake = ProductIntake(
        product_name="travel toiletry bag",
        business_context={},
        asset_media_summary="Compact zip organizer with clear compartments.",
    )

    output = runtime.run_video_scripting(
        run_id="test-tiktok-llm-success",
        variant_set=variant_set,
        intake=intake,
        business_context={
            "target_audience": "frequent travelers",
            "key_value_props": ["keeps bottles upright", "clear compartments"],
            "primary_cta": "Shop Now",
        },
        provider="openai",
        model="gpt-4.1",
        creative_specs={"tiktok_video_style": "ugc_demo", "video_duration_seconds": 12},
        pipeline_mode="tiktok_shop_video",
    )

    assert output.model_used == "gpt-4.1"
    assert ":fallback_to_template" not in output.model_used
    scripts = output.payload["scripts"]
    assert len(scripts) == 1
    assert scripts[0]["variant_id"] == "V1"
    assert scripts[0]["hook"] == "TikTok hook text"
    assert scripts[0]["script"] == "TikTok script body"
    assert scripts[0]["shot_list"] == ["TikTok shot 1", "TikTok shot 2"]

    # tiktok payload must be populated
    tiktok = scripts[0].get("tiktok")
    assert tiktok is not None, "tiktok field must be populated for tiktok_shop_video pipeline"
    assert isinstance(tiktok, dict)
    assert tiktok["style"] == "ugc_demo"
    assert "opening_hook" in tiktok
    assert isinstance(tiktok["on_screen_text"], list)
    assert isinstance(tiktok["voiceover_lines"], list)
    assert isinstance(tiktok["shot_timing"], list)
    assert isinstance(tiktok["product_proof_points"], list)
    assert tiktok["cta"] == "Shop Now"
    assert isinstance(tiktok["compliance_notes"], list)
    assert any("CTA intensity" in note for note in tiktok["compliance_notes"])


def test_tiktok_long_video_keeps_cta_to_final_seconds():
    runtime = AgentsRuntime()

    payload = runtime._build_tiktok_payload(
        product_name="olive satin dress",
        primary_value="one dress, three moods",
        cta="Shop the look",
        tiktok_style="direct_response_ad",
        video_duration=35,
    )

    cta_timing = payload["shot_timing"][-1]
    assert cta_timing["intent"] == "cta"
    assert cta_timing["end"] == 35
    assert cta_timing["end"] - cta_timing["start"] <= 4


# ---------------------------------------------------------------------------
# run_storyboard_image_generation LLM integration tests
# ---------------------------------------------------------------------------


def test_storyboard_llm_success_path_uses_llm_prompts():
    """When LLM returns valid JSON with frames, frame prompts come from the LLM output."""
    runtime = AgentsRuntime()

    llm_output = (
        '{"frames": ['
        '{"frame_id": "V1_F1", "variant_id": "V1", "prompt": "LLM storyboard prompt for frame 1"},'
        '{"frame_id": "V1_F2", "variant_id": "V1", "prompt": "LLM storyboard prompt for frame 2"},'
        '{"frame_id": "V1_F3", "variant_id": "V1", "prompt": "LLM storyboard prompt for frame 3"}'
        "]}"
    )
    runtime._chat_complete = lambda *args, **kwargs: (llm_output, "gpt-4.1", 0.05)

    def fake_generate_image(*, prompt, **kwargs):
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": []})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Pack faster without messy leaks",
                script="Show the travel toiletry bag holding upright bottles.",
                shot_list=[
                    "Open the bag on a hotel bathroom counter.",
                    "Close-up of upright bottle sleeves.",
                    "Packed bag slides neatly into a carry-on.",
                ],
            )
        ],
        product_context={"product_name": "travel toiletry bag", "audience": "frequent travelers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_storyboard_image_generation(
        run_id="test-storyboard-llm-success",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
    )

    assert output.model_used == "gpt-4.1"
    assert ":fallback_to_template" not in output.model_used
    frames = output.payload["frames"]
    assert len(frames) == 3
    assert frames[0]["prompt"] == "LLM storyboard prompt for frame 1"
    assert frames[1]["prompt"] == "LLM storyboard prompt for frame 2"
    assert frames[2]["prompt"] == "LLM storyboard prompt for frame 3"
    # Template content must NOT appear in LLM success path
    assert "Create a realistic storyboard frame" not in frames[0]["prompt"]


def test_storyboard_llm_parse_failure_falls_back_to_template():
    """When LLM JSON is unparseable, fall back to template prompts and tag model_used."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("not valid json", "gpt-4.1", 0.05)

    def fake_generate_image(*, prompt, **kwargs):
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": []})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Pack faster without messy leaks",
                script="Show the travel toiletry bag holding upright bottles.",
                shot_list=[
                    "Open the bag on a hotel bathroom counter.",
                    "Close-up of upright bottle sleeves.",
                    "Packed bag slides neatly into a carry-on.",
                ],
            )
        ],
        product_context={"product_name": "travel toiletry bag", "audience": "frequent travelers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_storyboard_image_generation(
        run_id="test-storyboard-llm-fallback",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
    )

    assert ":fallback_to_template" in output.model_used
    frames = output.payload["frames"]
    assert len(frames) == 3
    # Template content markers should appear in fallback path
    assert "Create a realistic storyboard frame" in frames[0]["prompt"]
    assert "travel toiletry bag" in frames[0]["prompt"].lower()


def test_storyboard_llm_empty_response_falls_back():
    """Empty LLM response triggers ValueError from _parse_llm_json and falls back."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("", "gpt-4.1", 0.05)

    def fake_generate_image(*, prompt, **kwargs):
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": []})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook text",
                script="Script body.",
                shot_list=["Shot 1", "Shot 2", "Shot 3"],
            )
        ],
        product_context={"product_name": "widget", "audience": "buyers"},
        generation_spec={},
    )

    output = runtime.run_storyboard_image_generation(
        run_id="test-storyboard-empty-response",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
    )

    assert ":fallback_to_template" in output.model_used
    assert len(output.payload["frames"]) == 3


def test_storyboard_llm_missing_frames_key_falls_back():
    """LLM JSON without 'frames' key falls back to template."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ('{"other_key": [1,2,3]}', "gpt-4.1", 0.05)

    def fake_generate_image(*, prompt, **kwargs):
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": []})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook text",
                script="Script body.",
                shot_list=["Shot 1", "Shot 2", "Shot 3"],
            )
        ],
        product_context={"product_name": "widget", "audience": "buyers"},
        generation_spec={},
    )

    output = runtime.run_storyboard_image_generation(
        run_id="test-storyboard-missing-key",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
    )

    assert ":fallback_to_template" in output.model_used
    assert len(output.payload["frames"]) == 3
    assert "Create a realistic storyboard frame" in output.payload["frames"][0]["prompt"]


def test_storyboard_llm_partial_frame_match_uses_llm_for_match_fallback_for_miss():
    """When LLM returns some frames but not all, matched frames use LLM prompts, others use template."""
    runtime = AgentsRuntime()

    llm_output = (
        '{"frames": ['
        '{"frame_id": "V1_F1", "variant_id": "V1", "prompt": "LLM frame 1 prompt"},'
        '{"frame_id": "V1_F3", "variant_id": "V1", "prompt": "LLM frame 3 prompt"}'
        "]}"
    )
    runtime._chat_complete = lambda *args, **kwargs: (llm_output, "gpt-4.1", 0.05)

    def fake_generate_image(*, prompt, **kwargs):
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": []})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook text",
                script="Script body.",
                shot_list=["Shot 1", "Shot 2", "Shot 3"],
            )
        ],
        product_context={"product_name": "widget", "audience": "buyers"},
        generation_spec={},
    )

    output = runtime.run_storyboard_image_generation(
        run_id="test-storyboard-partial-match",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
    )

    assert output.model_used == "gpt-4.1"
    assert ":fallback_to_template" not in output.model_used
    frames = output.payload["frames"]
    assert len(frames) == 3
    # Frame 1 and 3 should use LLM prompts
    assert frames[0]["prompt"] == "LLM frame 1 prompt"
    assert frames[2]["prompt"] == "LLM frame 3 prompt"
    # Frame 2 should use template (fallback)
    assert "Create a realistic storyboard frame" in frames[1]["prompt"]


def test_storyboard_chat_complete_exception_still_uses_template():
    """When _chat_complete itself raises, inner parse is skipped and template is used."""
    runtime = AgentsRuntime()

    def failing_chat_complete(*args, **kwargs):
        raise RuntimeError("API transport failure")

    runtime._chat_complete = failing_chat_complete

    def fake_generate_image(*, prompt, **kwargs):
        return (
            type("ImageResult", (), {"estimated_cost": 0.0, "images": []})(),
            "stub-image-provider",
            "stub-image-model",
        )

    runtime._generate_image = fake_generate_image

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook text",
                script="Script body.",
                shot_list=["Shot 1", "Shot 2", "Shot 3"],
            )
        ],
        product_context={"product_name": "widget", "audience": "buyers"},
        generation_spec={},
    )

    output = runtime.run_storyboard_image_generation(
        run_id="test-storyboard-transport-failure",
        script_pack=script_pack,
        provider="openai",
        model="gpt-4.1",
        creative_specs={"video_size": "9:16"},
    )

    assert ":storyboard_text_unavailable" in output.model_used
    assert ":fallback_to_template" not in output.model_used
    frames = output.payload["frames"]
    assert len(frames) == 3
    assert "Create a realistic storyboard frame" in frames[0]["prompt"]
    # Error should be set on frames
    assert frames[0]["error"] == "API transport failure"


# ---------------------------------------------------------------------------
# run_video_generation LLM integration tests
# ---------------------------------------------------------------------------


def test_video_generation_llm_success_path_uses_llm_prompts():
    """When LLM returns valid JSON with video_prompts, use LLM's prompt for video generation."""
    runtime = AgentsRuntime()

    llm_output = (
        '{"video_prompts": ['
        '{"variant_id": "V1", "prompt": "LLM optimized video prompt for V1", "quality_constraints": ["keep product centered"]},'
        '{"variant_id": "V2", "prompt": "LLM optimized video prompt for V2", "quality_constraints": ["maintain lighting consistency"]}'
        "]}"
    )
    runtime._chat_complete = lambda *args, **kwargs: (llm_output, "gpt-4.1", 0.05)

    fake_provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(fake_provider)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook one",
                script="Script one.",
                shot_list=["Shot A", "Shot B"],
            ),
            VideoScriptItem(
                variant_id="V2",
                hook="Hook two",
                script="Script two.",
                shot_list=["Shot C", "Shot D"],
            ),
        ],
        product_context={"product_name": "test product", "audience": "testers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_video_generation(
        run_id="test-video-gen-llm-success",
        script_pack=script_pack,
        creative_specs={},
        provider="apimart",
        model="doubao-seedance-2.0",
    )

    assert ":fallback_to_template" not in output.model_used
    assert "text=gpt-4.1" in output.model_used
    videos = output.payload["videos"]
    assert len(videos) == 2
    # V1 should use LLM prompt
    assert videos[0]["prompt"] == "LLM optimized video prompt for V1"
    assert videos[0]["variant_id"] == "V1"
    # V2 should use LLM prompt
    assert videos[1]["prompt"] == "LLM optimized video prompt for V2"
    assert videos[1]["variant_id"] == "V2"
    # Template-specific phrasing MUST NOT appear in LLM success path
    assert "Generate a short social ad video clip based on script" not in videos[0]["prompt"]


def test_video_generation_llm_parse_failure_falls_back_to_template():
    """When LLM JSON is unparseable, fall back to template prompts and tag model_used."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("not valid json", "gpt-4.1", 0.05)

    fake_provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(fake_provider)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Pack faster without messy leaks",
                script="Show the travel toiletry bag holding upright bottles.",
                shot_list=["Open the bag", "Close-up of bottles", "Slide into carry-on"],
            )
        ],
        product_context={"product_name": "travel toiletry bag", "audience": "frequent travelers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_video_generation(
        run_id="test-video-gen-llm-fallback",
        script_pack=script_pack,
        creative_specs={},
        provider="apimart",
        model="doubao-seedance-2.0",
    )

    assert ":fallback_to_template" in output.model_used
    videos = output.payload["videos"]
    assert len(videos) == 1
    # Template content markers should appear in fallback path
    assert "Generate a short social ad video clip based on script" in videos[0]["prompt"]
    assert "travel toiletry bag" in videos[0]["prompt"].lower()


def test_video_generation_continues_when_prompt_llm_fails():
    runtime = AgentsRuntime()

    def fail_chat(*args, **kwargs):
        raise RuntimeError("text model unavailable")

    runtime._chat_complete = fail_chat
    fake_provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(fake_provider)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook text",
                script="Script body.",
                shot_list=["Shot 1", "Shot 2"],
            )
        ],
        product_context={"product_name": "test product", "audience": "testers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_video_generation(
        run_id="test-video-gen-text-fallback",
        script_pack=script_pack,
        storyboard_frames=None,
        creative_specs={},
        provider="deepseek",
        model="deepseek-v4-pro",
        runtime_config={"video": {"provider_name": "apimart", "model_name": "doubao-seedance-2.0"}},
    )

    assert fake_provider.last_request is not None
    assert "text=deepseek-v4-pro:fallback_to_template" in output.model_used
    assert "video=doubao-seedance-2.0" in output.model_used
    assert output.payload["videos"][0]["generation_status"] == "submitted"
    assert output.payload["videos"][0]["external_task_state"]["task_id"] == "task-video-001"
    assert output.payload["videos"][0]["external_task_state"]["asset_type"] == "video"
    assert output.payload["videos"][0]["external_task_state"]["poll_count"] == 0


def test_video_generation_storyboard_frames_inject_image_urls():
    """storyboard_frames image_uri values are converted to data URLs and injected into generation_spec."""
    import base64
    import os
    import tempfile

    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    fake_provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(fake_provider)

    # Create a real temporary image file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        tmp_path = tmp.name

    try:
        storyboard_frames = [
            {"frame_id": "V1_F1", "variant_id": "V1", "image_uri": tmp_path, "prompt": "frame prompt"},
        ]

        script_pack = VideoScriptPack(
            scripts=[
                VideoScriptItem(
                    variant_id="V1",
                    hook="Hook text",
                    script="Script body.",
                    shot_list=["Shot 1", "Shot 2"],
                )
            ],
            product_context={"product_name": "test product", "audience": "testers"},
            generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
        )

        output = runtime.run_video_generation(
            run_id="test-video-gen-storyboard-inject",
            script_pack=script_pack,
            storyboard_frames=storyboard_frames,
            creative_specs={},
            provider="apimart",
            model="doubao-seedance-2.0",
        )

        # The generation_spec should contain image_urls with the data URL
        video_payload = output.payload["videos"][0]
        gen_spec = video_payload.get("generation_spec", {})
        image_urls = gen_spec.get("image_urls", [])
        assert len(image_urls) >= 1
        data_url = image_urls[0]
        assert data_url.startswith("data:image/png;base64,")
        # Verify it's a valid base64 data URL
        b64_part = data_url.split(",", 1)[1]
        decoded = base64.b64decode(b64_part)
        assert len(decoded) > 0
    finally:
        os.unlink(tmp_path)


def test_video_generation_storyboard_frames_prefer_provider_urls():
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    fake_provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(fake_provider)

    storyboard_frames = [
        {
            "frame_id": "V1_F1",
            "variant_id": "V1",
            "image_uri": "/nonexistent/local-copy.png",
            "raw_response": {"data": [{"url": "https://getapib.org/tokens/storyboard-frame.png"}]},
        },
    ]
    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook text",
                script="Script body.",
                shot_list=["Shot 1", "Shot 2"],
            )
        ],
        product_context={"product_name": "test product", "audience": "testers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_video_generation(
        run_id="test-video-gen-storyboard-provider-url",
        script_pack=script_pack,
        storyboard_frames=storyboard_frames,
        creative_specs={},
        provider="apimart",
        model="doubao-seedance-2.0",
    )

    video_payload = output.payload["videos"][0]
    assert video_payload["generation_spec"]["image_urls"] == ["https://getapib.org/tokens/storyboard-frame.png"]


def test_video_generation_storyboard_frames_none_unchanged():
    """When storyboard_frames is None, image_urls from creative_specs are preserved unchanged."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    fake_provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(fake_provider)

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook text",
                script="Script body.",
                shot_list=["Shot 1", "Shot 2"],
            )
        ],
        product_context={"product_name": "test product", "audience": "testers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_video_generation(
        run_id="test-video-gen-no-storyboard",
        script_pack=script_pack,
        creative_specs={
            "image_urls": ["https://example.com/reference-image.png"],
        },
        provider="apimart",
        model="doubao-seedance-2.0",
    )

    # The generation_spec should contain only the creative_specs image_urls
    video_payload = output.payload["videos"][0]
    gen_spec = video_payload.get("generation_spec", {})
    image_urls = gen_spec.get("image_urls", [])
    assert image_urls == ["https://example.com/reference-image.png"]


def test_video_generation_storyboard_frame_file_missing_skipped():
    """storyboard_frames with non-existent image_uri paths are skipped, not injected."""
    runtime = AgentsRuntime()
    runtime._chat_complete = lambda *args, **kwargs: ("ok", "stub-model", 0.0)

    fake_provider = _FakeVideoProvider()
    runtime.providers = _FakeRegistry(fake_provider)

    storyboard_frames = [
        {"frame_id": "V1_F1", "variant_id": "V1", "image_uri": "/nonexistent/path/frame.png", "prompt": "frame prompt"},
    ]

    script_pack = VideoScriptPack(
        scripts=[
            VideoScriptItem(
                variant_id="V1",
                hook="Hook text",
                script="Script body.",
                shot_list=["Shot 1", "Shot 2"],
            )
        ],
        product_context={"product_name": "test product", "audience": "testers"},
        generation_spec={"size": "16:9", "resolution": "720p", "duration": 5},
    )

    output = runtime.run_video_generation(
        run_id="test-video-gen-frame-missing",
        script_pack=script_pack,
        storyboard_frames=storyboard_frames,
        creative_specs={
            "image_urls": ["https://example.com/reference-image.png"],
        },
        provider="apimart",
        model="doubao-seedance-2.0",
    )

    # The generation_spec should only retain the creative_specs image_urls
    # (the missing frame file was skipped by _local_image_to_data_url)
    video_payload = output.payload["videos"][0]
    gen_spec = video_payload.get("generation_spec", {})
    image_urls = gen_spec.get("image_urls", [])
    assert image_urls == ["https://example.com/reference-image.png"]
