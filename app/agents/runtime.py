from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.agents.persona_contracts import render_persona_prompt
from app.providers.llm import (
    GeneratedVideo,
    ImageGenRequest,
    MultimodalChatRequest,
    ProviderRegistry,
    VideoGenRequest,
    decode_placeholder_png,
)
from app.providers.media import LocalMediaProvider
from app.services.creative_specs import (
    get_dtc_site_review_hints,
    get_dtc_site_surface_strategy,
    normalize_storyboard_candidate_count,
)
from app.services.marketplace_qa import (
    build_product_truth_contract,
    infer_visual_identity,
    inspect_marketplace_image,
    is_marketplace_main_image,
    normalize_platform_targets,
)
from app.services.video_frames import extract_last_video_frame, sample_video_frames, stitch_video_files
from app.services.visual_qa import inspect_extracted_video_frames, inspect_visual_asset
from app.schemas.contracts import (
    ComplianceLevel,
    ConversionForecast,
    CopyImageBundle,
    CopyVariant,
    EvaluationResult,
    ImageAssetRef,
    PlanningBrief,
    ProductIntake,
    ProductVisualIdentity,
    RankedVariant,
    ScoreBreakdown,
    ScoreCard,
    SelectedDeliverables,
    ShotFramePlan,
    ShotPlanItem,
    VariantCandidate,
    VariantSet,
    VideoAsset,
    VideoBundle,
    VideoSegmentPlan,
    VideoScriptItem,
    VideoScriptPack,
)


@dataclass(slots=True)
class StageOutput:
    payload: dict
    model_used: str | None = None
    estimated_cost: float = 0.0
    artifacts: list[dict] = field(default_factory=list)
    scorecard: ScoreCard | None = None
    forecast: ConversionForecast | None = None


def _gm_lesson_angle_priorities(gm_lessons: list[dict]) -> list[str]:
    angles: list[str] = []
    for lesson in gm_lessons:
        content = lesson.get("content") or {}
        candidates = list(content.get("winning_patterns") or [])
        candidates.extend(item.get("pattern") or {} for item in (content.get("top_variants") or []))
        for item in candidates:
            if isinstance(item, dict):
                value = item.get("angle") or item.get("hook") or item.get("message")
            else:
                value = item
            if value and str(value).strip():
                angles.append(str(value).strip())
        summary = content.get("summary")
        if summary and not angles:
            angles.append(str(summary).strip()[:160])
    return list(dict.fromkeys(angles))


class AgentsRuntime:
    def __init__(self) -> None:
        self.providers = ProviderRegistry()
        self.media = LocalMediaProvider()

    def _utc_now_iso(self) -> str:
        return datetime.now(UTC).isoformat()

    def expand_image_prompt(self, idea: str, runtime_config: dict) -> str:
        instruction = (
            "Turn the supplied visual idea into one finished image-generation prompt, ready to paste into an image model. "
            "Write in the language requested, otherwise match the idea's language. Return only the prompt. "
            "Preserve named subjects, count, wording, medium, ratio, brand facts and exclusions. "
            "Make coherent art-direction choices for unspecified details: describe the subject doing something visible, "
            "its relationship to its setting, placement within the crop, foreground and background, and the focal detail. "
            "For a photograph choose a viewpoint and lighting direction that reveal the subject. For illustration or graphic work "
            "describe line, shape, texture and palette in the requested medium. Tie choices to the intention of this particular image. "
            "When copy is requested, preserve exact supplied text and choose readable placement. When space for later copy is requested, "
            "describe that clear area. Treat proposed styling as direction; do not invent product features, labels, results or testimonials. "
            "Avoid generic quality tags, contradictory instructions, placeholders, commentary and follow-up questions. "
            "The visual brief is supplied as JSON:\n" + json.dumps({"idea": idea}, ensure_ascii=False)
        )
        result, _, _ = self._chat_complete(
            runtime_config["provider_name"], runtime_config["model_name"], instruction, runtime_config
        )
        if not result.strip():
            raise ValueError("Empty image prompt")
        return result.strip()

    def _external_task_state(
        self,
        *,
        asset_type: str,
        task_id: str | None,
        status: str | None,
        provider_name: str,
        model_name: str,
        event: str,
    ) -> dict | None:
        if not task_id:
            return None
        now = self._utc_now_iso()
        return {
            "asset_type": asset_type,
            "task_id": task_id,
            "status": status or "submitted",
            "provider_name": provider_name,
            "model_name": model_name,
            "submitted_at": now,
            "last_seen_at": now,
            "last_event": event,
            "poll_count": 0,
        }

    def _trace_external_task(
        self,
        runtime_config: dict | None,
        *,
        event_type: str,
        message: str,
        payload: dict,
    ) -> None:
        trace_callback = (runtime_config or {}).get("trace_callback")
        if trace_callback:
            trace_callback(event_type, message, payload)

    def _director_handoff(self, planning: PlanningBrief | dict | None) -> dict:
        if not planning:
            return {}
        payload = planning.model_dump() if hasattr(planning, "model_dump") else dict(planning)
        handoff = {
            "creative_director_plan": payload.get("creative_director_plan") or {},
            "production_plan": payload.get("production_plan") or {},
            "quality_gates": payload.get("quality_gates") or [],
        }
        return handoff if any(handoff.values()) else {}

    def _scene_arc_item(self, director_handoff: dict, idx: int) -> dict:
        scene_arc = (director_handoff.get("creative_director_plan") or {}).get("scene_arc") or []
        if not scene_arc:
            return {}
        item = scene_arc[idx % len(scene_arc)]
        return item if isinstance(item, dict) else {"scene_direction": str(item)}

    def _trace_provider_selection(
        self,
        runtime_config: dict | None,
        *,
        capability: str,
        selected_provider: str,
        selected_model: str,
        fallback_provider: str,
        fallback_model: str,
        operation: str,
        reason: str,
        extra: dict | None = None,
    ) -> None:
        trace_callback = (runtime_config or {}).get("trace_callback")
        if not trace_callback:
            return
        alternatives = []
        fallback = f"{fallback_provider}/{fallback_model}"
        selected = f"{selected_provider}/{selected_model}"
        if fallback != selected:
            alternatives.append(fallback)
        trace_callback(
            "provider_selection",
            f"Selected {selected_provider}/{selected_model} for {capability}.",
            {
                "decision_type": "generation_provider_selection",
                "capability": capability,
                "operation": operation,
                "selected": selected,
                "selected_provider_name": selected_provider,
                "selected_model_name": selected_model,
                "fallback": fallback,
                "options_considered": [selected, *alternatives],
                "reason": reason,
                **(extra or {}),
            },
        )

    def _chat_complete(
        self,
        provider: str,
        model: str,
        prompt: str,
        runtime_config: dict | None,
        *,
        image_urls: list[str] | None = None,
        video_urls: list[str] | None = None,
    ) -> tuple[str, str, float]:
        llm = self.providers.get(provider)
        runtime = runtime_config or {}
        extra = dict(runtime.get("extra") or {})
        for key in ("thinking_mode", "thinking_budget_tokens", "max_output_tokens", "request_timeout_seconds"):
            if runtime.get(key) is not None and key not in extra:
                extra[key] = runtime.get(key)
        streaming_enabled = bool(runtime.get("streaming_enabled") or extra.get("streaming_enabled"))
        trace_callback = runtime.get("trace_callback")
        if streaming_enabled:
            text_chunks: list[str] = []
            model_used = model
            estimated_cost = 0.0
            try:
                for event in llm.chat_complete_stream(
                    MultimodalChatRequest(
                        prompt=prompt,
                        model=model,
                        image_urls=image_urls or [],
                        video_urls=video_urls or [],
                    ),
                    api_base_url=runtime.get("api_base_url"),
                    api_key=runtime.get("api_key"),
                    extra=extra,
                ):
                    if event.type == "text_delta" and event.text:
                        text_chunks.append(event.text)
                        if trace_callback:
                            trace_callback("model_delta", event.text, {"event_type": event.type})
                    elif event.type == "reasoning_summary" and event.text and trace_callback:
                        trace_callback("reasoning_summary", event.text, {"event_type": event.type})
                    elif event.type == "completed":
                        model_used = str(event.payload.get("model") or model_used)
                        estimated_cost = float(event.payload.get("estimated_cost") or 0.0)
                        if event.payload.get("text") and not text_chunks:
                            text_chunks.append(str(event.payload["text"]))
                        if trace_callback:
                            trace_callback("model_stream_completed", "Model stream completed.", event.payload)
                return "".join(text_chunks), model_used, estimated_cost
            except Exception:
                if trace_callback:
                    trace_callback("model_stream_fallback", "Streaming failed; falling back to non-streaming call.", {})
        response = llm.chat_complete(
            MultimodalChatRequest(
                prompt=prompt,
                model=model,
                image_urls=image_urls or [],
                video_urls=video_urls or [],
            ),
            api_base_url=runtime.get("api_base_url"),
            api_key=runtime.get("api_key"),
            extra=extra,
        )
        return response.text, response.model_used, response.estimated_cost

    def _generate_image(
        self,
        *,
        fallback_provider: str,
        fallback_model: str,
        prompt: str,
        size: str,
        runtime_config: dict | None,
        reference_image_urls: list[str] | None = None,
        mode: str = "generate",
        input_fidelity: str | None = None,
        official_fallback: bool | None = None,
    ):
        runtime = runtime_config or {}
        image_runtime = runtime.get("image") or {}
        provider_name = image_runtime.get("provider_name") or fallback_provider
        model_name = image_runtime.get("model_name") or fallback_model
        api_base_url = image_runtime.get("api_base_url") or runtime.get("api_base_url")
        image_extra = dict(image_runtime.get("extra") or runtime.get("extra") or {})
        if (
            "apimart" in f"{provider_name} {api_base_url}".lower()
            and model_name == "gpt-image-2"
            and "submit_only" not in image_extra
            and "image_poll_max_wait_seconds" not in image_extra
        ):
            image_extra["submit_only"] = True
        self._trace_provider_selection(
            runtime_config,
            capability="image_generation",
            selected_provider=provider_name,
            selected_model=model_name,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
            operation=mode,
            reason="Using image-specific runtime config." if image_runtime.get("provider_name") or image_runtime.get("model_name") else "Using stage text model fallback for image generation.",
            extra={
                "has_reference_images": bool(reference_image_urls),
                "reference_image_count": len(reference_image_urls or []),
                "size": size,
            },
        )
        llm = self.providers.get(provider_name)
        result = llm.generate_image(
            ImageGenRequest(
                model=model_name,
                prompt=prompt,
                n=1,
                size=size,
                image_urls=reference_image_urls or [],
                reference_image_urls=reference_image_urls or [],
                mode=mode,
                input_fidelity=input_fidelity,
                official_fallback=official_fallback,
            ),
            api_base_url=api_base_url,
            api_key=image_runtime.get("api_key") or runtime.get("api_key"),
            extra=image_extra,
        )
        return result, provider_name, model_name

    def _generate_video_submit_only(
        self,
        *,
        fallback_provider: str,
        fallback_model: str,
        prompt: str,
        size: str,
        resolution: str,
        duration_seconds: int,
        video_payload: dict | None,
        runtime_config: dict | None,
    ):
        runtime = runtime_config or {}
        video_runtime = dict(runtime.get("video") or {})
        video_extra = dict(video_runtime.get("extra") or runtime.get("extra") or {})
        video_extra["submit_only"] = True
        video_runtime["extra"] = video_extra
        submit_runtime = {**runtime, "video": video_runtime}
        return self._generate_video(
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
            prompt=prompt,
            size=size,
            resolution=resolution,
            duration_seconds=duration_seconds,
            video_payload=video_payload,
            runtime_config=submit_runtime,
        )

    def _generate_video(
        self,
        *,
        fallback_provider: str,
        fallback_model: str,
        prompt: str,
        size: str,
        resolution: str,
        duration_seconds: int,
        video_payload: dict | None,
        runtime_config: dict | None,
    ):
        runtime = runtime_config or {}
        video_runtime = runtime.get("video") or {}
        provider_name = video_runtime.get("provider_name") or fallback_provider
        model_name = video_runtime.get("model_name") or fallback_model
        self._trace_provider_selection(
            runtime_config,
            capability="video_generation",
            selected_provider=provider_name,
            selected_model=model_name,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
            operation="submit_only" if (video_runtime.get("extra") or {}).get("submit_only") else "generate",
            reason="Using video-specific runtime config." if video_runtime.get("provider_name") or video_runtime.get("model_name") else "Using stage text model fallback for video generation.",
            extra={
                "size": size,
                "resolution": resolution,
                "duration_seconds": duration_seconds,
                "has_image_references": bool((video_payload or {}).get("image_urls") or (video_payload or {}).get("image_with_roles")),
            },
        )
        llm = self.providers.get(provider_name)
        extra = dict(video_runtime.get("extra") or runtime.get("extra") or {})
        video_payload = video_payload or {}
        result = llm.generate_video(
            VideoGenRequest(
                model=model_name,
                prompt=prompt,
                size=size,
                resolution=resolution,
                n=1,
                duration_seconds=duration_seconds,
                seed=video_payload.get("seed"),
                generate_audio=video_payload.get("generate_audio"),
                return_last_frame=video_payload.get("return_last_frame"),
                tools=list(video_payload.get("tools") or []),
                image_urls=list(video_payload.get("image_urls") or []),
                image_with_roles=list(video_payload.get("image_with_roles") or []),
                video_urls=list(video_payload.get("video_urls") or []),
                audio_urls=list(video_payload.get("audio_urls") or []),
            ),
            api_base_url=video_runtime.get("api_base_url") or runtime.get("api_base_url"),
            api_key=video_runtime.get("api_key") or runtime.get("api_key"),
            extra=extra,
        )
        return result, provider_name, model_name

    def _local_image_to_data_url(self, path_str: str) -> str | None:
        path = Path(path_str)
        if not path.exists() or not path.is_file():
            return None
        raw = path.read_bytes()
        if not raw:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _local_image_to_review_data_url(self, path_str: str, *, max_side: int = 1024, quality: int = 82) -> str | None:
        path = Path(path_str)
        if not path.exists() or not path.is_file():
            return None
        try:
            from PIL import Image  # type: ignore
        except Exception:
            return self._local_image_to_data_url(path_str)
        try:
            from io import BytesIO

            with Image.open(path) as image:
                image = image.convert("RGB")
                image.thumbnail((max_side, max_side))
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                raw = buffer.getvalue()
        except Exception:
            return self._local_image_to_data_url(path_str)
        if not raw:
            return None
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def _provider_reference_url(self, value: object) -> str | None:
        url = str(value or "").strip()
        return url if url.startswith(("http://", "https://", "asset://")) else None

    def _video_runtime_requires_hosted_references(
        self,
        *,
        fallback_provider: str,
        fallback_model: str,
        runtime_config: dict | None,
    ) -> bool:
        video_runtime = (runtime_config or {}).get("video") if isinstance(runtime_config, dict) else None
        video_runtime = video_runtime if isinstance(video_runtime, dict) else {}
        provider_name = str(video_runtime.get("provider_name") or fallback_provider or "").lower()
        model_name = str(video_runtime.get("model_name") or fallback_model or "").lower()
        api_base_url = str(video_runtime.get("api_base_url") or "").lower()
        return ("apimart" in provider_name or "apimart" in api_base_url) and model_name.startswith("doubao-seedance")

    def _segment_image_reference_payload(
        self,
        base_image_urls: list[str] | None,
        bridge_frame_uri: str | None,
        *,
        max_reference_images: int = 9,
        allow_data_urls: bool = True,
    ) -> tuple[dict, str, list[dict]]:
        manifest: list[dict] = []
        max_refs = max(1, min(9, int(max_reference_images or 9)))
        refs: list[str] = []
        used_tail_frame = False

        def add_manifest(
            *,
            uri: str,
            source: str,
            role: str,
            provider_usable: bool,
            used: bool = False,
            reason: str | None = None,
            transport: str | None = None,
        ) -> None:
            row = {
                "uri": uri,
                "source": source,
                "role": role,
                "provider_usable": provider_usable,
                "used": used,
            }
            if reason:
                row["reason"] = reason
            if transport:
                row["transport"] = transport
            manifest.append(row)

        def add_ref(uri: str, *, source: str, role: str, transport: str) -> bool:
            if len(refs) >= max_refs:
                add_manifest(
                    uri=uri,
                    source=source,
                    role=role,
                    provider_usable=True,
                    used=False,
                    reason="max_reference_images_exceeded",
                    transport=transport,
                )
                return False
            refs.append(uri)
            add_manifest(uri=uri, source=source, role=role, provider_usable=True, used=True, transport=transport)
            return True

        if bridge_frame_uri:
            bridge_url = self._provider_reference_url(bridge_frame_uri)
            if bridge_url:
                add_manifest(
                    uri=bridge_url,
                    source="tail_frame",
                    role="first_frame",
                    provider_usable=True,
                    used=True,
                    transport="hosted_url",
                )
                return {"image_with_roles": [{"url": bridge_url, "role": "first_frame"}]}, "first_frame", manifest
            bridge_data_url = self._local_image_to_data_url(bridge_frame_uri) if allow_data_urls else None
            if bridge_data_url:
                used_tail_frame = add_ref(bridge_data_url, source="tail_frame", role="first_frame", transport="data_url")
            else:
                add_manifest(
                    uri=str(bridge_frame_uri),
                    source="tail_frame",
                    role="first_frame",
                    provider_usable=False,
                    reason="requires_hosted_reference" if not allow_data_urls else "unreadable_reference",
                )

        for item in base_image_urls or []:
            uri = str(item or "").strip()
            if not uri:
                continue
            hosted_url = self._provider_reference_url(uri)
            if hosted_url:
                add_ref(hosted_url, source="base_image", role="anchor", transport="hosted_url")
                continue
            if uri.startswith("data:") and allow_data_urls:
                add_ref(uri, source="base_image", role="anchor", transport="data_url")
                continue
            add_manifest(
                uri=uri,
                source="base_image",
                role="anchor",
                provider_usable=False,
                reason="requires_hosted_reference" if not allow_data_urls else "unsupported_reference_uri",
            )

        if refs:
            return {"image_urls": refs}, "tail_with_anchors" if used_tail_frame else "anchors", manifest
        return {}, "", manifest

    def _last_frame_url_from_raw(self, *payloads: dict | None) -> str | None:
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for key in ("last_frame_url", "last_frame", "last_frame_image_url"):
                url = self._provider_reference_url(payload.get(key))
                if url:
                    return url
        return None

    def _generated_image_url_from_raw(self, *payloads: dict | None) -> str | None:
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for key in ("result_url", "image_url", "url"):
                url = self._provider_reference_url(payload.get(key))
                if url:
                    return url
            data = payload.get("data")
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    url = self._provider_reference_url(item.get("url") or item.get("image_url"))
                    if url:
                        return url
        return None

    def _storyboard_frame_reference_url(self, frame: dict) -> str | None:
        url = self._generated_image_url_from_raw(frame, frame.get("raw_response") if isinstance(frame, dict) else None)
        if url:
            return url
        candidates = frame.get("candidate_frames") if isinstance(frame, dict) else None
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                url = self._generated_image_url_from_raw(candidate, candidate.get("raw_response"))
                if url:
                    return url
        return None

    def _segment_bridge_instruction(self, segment_index: int, has_bridge_frame: bool) -> str:
        if segment_index == 0:
            return "Start the long-form ad once; this segment is the only opening hook. Use supplied reference images as product and model identity anchors."
        if has_bridge_frame:
            return "Continue the exact action from the supplied first_frame reference; do not restart with a new intro. Preserve the same person, face, styling, product silhouette, color, fabric, and details from the established ad."
        return "Continue the prior scene emotionally and visually; do not restart with a new intro. Preserve the same person and product identity from the reference anchors."

    def _human_integrity_instruction(self, text: str) -> str:
        if not re.search(r"\b(model|woman|man|person|people|human|girl|boy|female|male|she|he|her|his|wearing|face|arm|hand|body)\b|模特|真人|人物|女性|男性", text, re.IGNORECASE):
            return ""
        return (
            "Human anatomy constraint: if any person/model appears, keep both arms, hands, shoulders, and legs visible or naturally occluded; "
            "no missing, cropped, fused, extra, or deformed limbs; keep hands and arms physically plausible during motion. "
        )

    def _mentions_human_subject(self, text: object) -> bool:
        return bool(self._human_integrity_instruction(str(text or "")))

    def _human_motion_risk_instruction(self, text: object) -> str:
        if not self._mentions_human_subject(text):
            return ""
        return "For people/model shots, prefer slow stable poses, controlled 3/4 turns, or gentle walking; avoid fast twirls, spins, heavy occlusion, crossed limbs, or motion that hides arms/hands unless essential."

    def _local_video_to_data_url(self, path_str: str, *, max_bytes: int = 20 * 1024 * 1024) -> str | None:
        path = Path(path_str)
        if not path.exists() or not path.is_file():
            return None
        raw = path.read_bytes()
        if not raw:
            return None
        if len(raw) > max_bytes:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        if not mime.startswith("video/"):
            mime = "video/mp4"
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _reference_image_inputs(
        self,
        intake: ProductIntake | None,
        extra_references: list[dict] | None = None,
    ) -> list[str]:
        if not intake:
            return []
        rows = intake.image_references or []
        inputs: list[str] = []
        for row in rows[:2]:
            if not isinstance(row, dict):
                continue
            uri = row.get("uri")
            if not isinstance(uri, str):
                continue
            data_url = self._local_image_to_data_url(uri)
            if data_url:
                inputs.append(data_url)
        # Append extra references (e.g., historical best images), cap at 4 total
        for ref in (extra_references or [])[:2]:
            data_url = ref.get("uri")
            if isinstance(data_url, str) and data_url:
                if len(inputs) >= 4:
                    break
                inputs.append(data_url)
        return inputs

    def _reference_video_inputs(self, intake: ProductIntake | None) -> list[str]:
        if not intake:
            return []
        rows = intake.video_references or []
        inputs: list[str] = []
        for row in rows[:1]:
            if not isinstance(row, dict):
                continue
            uri = row.get("uri")
            if not isinstance(uri, str):
                continue
            data_url = self._local_video_to_data_url(uri)
            if data_url:
                inputs.append(data_url)
        return inputs

    def _marketplace_reference_inputs(self, intake: ProductIntake | None, max_inputs: int = 4) -> tuple[list[str], list[dict]]:
        if not intake:
            return [], []
        identity = intake.visual_identity.model_dump() if hasattr(intake.visual_identity, "model_dump") else dict(intake.visual_identity or {})
        candidates: list[dict] = []
        for uri in identity.get("best_reference_images") or []:
            candidates.append({"uri": uri, "source": "visual_identity_image"})
        for uri in identity.get("best_reference_frames") or []:
            candidates.append({"uri": uri, "source": "visual_identity_video_frame"})
        for row in intake.image_references or []:
            if isinstance(row, dict) and row.get("uri"):
                candidates.append({"uri": row["uri"], "source": "uploaded_image"})
        for row in intake.video_references or []:
            if not isinstance(row, dict):
                continue
            for uri in row.get("frame_placeholders") or row.get("frame_uris") or []:
                candidates.append({"uri": uri, "source": "uploaded_video_frame", "source_video": row.get("uri")})

        data_urls: list[str] = []
        manifest: list[dict] = []
        seen: set[str] = set()
        for item in candidates:
            uri = item.get("uri")
            if not isinstance(uri, str) or not uri or uri in seen:
                continue
            seen.add(uri)
            data_url = self._local_image_to_data_url(uri)
            if not data_url:
                continue
            data_urls.append(data_url)
            manifest.append({**item, "input_index": len(manifest) + 1})
            if len(data_urls) >= max_inputs:
                break
        return data_urls, manifest

    def _product_truth_contract(self, intake: ProductIntake | None) -> dict:
        if not intake:
            return {}
        if isinstance(intake.product_truth_contract, dict) and intake.product_truth_contract:
            return intake.product_truth_contract
        visual_identity = intake.visual_identity.model_dump() if hasattr(intake.visual_identity, "model_dump") else dict(intake.visual_identity or {})
        return build_product_truth_contract(
            product_name=intake.product_name,
            visual_identity=visual_identity,
            sku_summary=intake.sku_summary,
        )

    def _planning_prompt_grammar(
        self,
        *,
        product_name: str,
        brief: str,
        media_truth: str,
        creative_specs: dict,
        constraints: list[str],
        product_truth: dict,
    ) -> dict:
        source = " ".join([brief, media_truth, json.dumps(creative_specs, ensure_ascii=False)]).lower()
        is_documentary = any(
            term in source
            for term in (
                "documentary",
                "home-video",
                "home video",
                "camcorder",
                "early-2000",
                "handheld",
                "candid",
                "ugc",
                "vlog",
                "实拍",
                "手持",
                "纪实",
            )
        )
        is_pov = "pov" in source or "第一人称" in source
        is_sticker = any(term in source for term in ("2d", "sticker", "anime", "cartoon", "贴纸", "动漫", "卡通", "纸片"))

        camera_style = "vertical handheld UGC camera with natural micro-shake and product-readable framing"
        visual_style = "realistic product-use video with concrete setting, visible product proof, and no generic beauty-shot filler"
        if is_documentary:
            visual_style = "documentary realism / candid home-video realism with ordinary real-world details"
            camera_style = (
                "handheld documentary capture with drift, imperfect framing, autofocus hunting, exposure shifts, "
                "mild motion blur, and no cinematic stabilization"
            )
        if is_pov:
            camera_style = f"first-person POV; {camera_style}"
        if is_sticker:
            visual_style = "live-action base footage plus flat 2D sticker/comic layer"

        duration = int(creative_specs.get("video_duration_seconds") or creative_specs.get("duration") or 8)
        audio_plan = "natural on-set audio: footsteps, product handling, room tone, and light ambient environment"
        if is_documentary:
            audio_plan = "natural ambient sound only; no music, no narration unless the brief explicitly asks for it"

        negative_constraints = list(dict.fromkeys(
            [
                *[str(item) for item in constraints if str(item).strip()],
                *[str(item) for item in product_truth.get("forbidden_changes", []) if str(item).strip()],
                "no generic lifestyle scene without visible product proof",
            ]
        ))
        if is_documentary:
            negative_constraints.extend(["no cinematic camera moves", "no modern glossy color grading"])
        if is_sticker:
            negative_constraints.extend(["do not relight the flat 2D layer", "do not turn sticker layer into 3D"])

        return {
            "visual_style": visual_style,
            "camera_style": camera_style,
            "timeline_density": f"{duration}s timeline with timestamped beats; each beat names action, product proof, camera behavior, and audio cue",
            "audio_plan": audio_plan,
            "negative_constraints": list(dict.fromkeys(negative_constraints)),
            "layer_rules": "real footage only" if not is_sticker else "real kitchen/scene layer stays live-action; sticker/comic layer stays flat 2D",
            "source_reference_policy": f"adapt craft principles to {product_name}; do not copy unrelated subjects, jokes, or settings",
        }

    def _prompt_grammar_text(self, prompt_grammar: dict | None) -> str:
        if not prompt_grammar:
            return ""
        negatives = ", ".join(str(item) for item in (prompt_grammar.get("negative_constraints") or [])[:6])
        parts = [
            f"visual style: {prompt_grammar.get('visual_style')}",
            f"camera: {prompt_grammar.get('camera_style')}",
            f"audio: {prompt_grammar.get('audio_plan')}",
            f"timeline: {prompt_grammar.get('timeline_density')}",
        ]
        if negatives:
            parts.append(f"avoid: {negatives}")
        layer_rules = str(prompt_grammar.get("layer_rules") or "").strip()
        if layer_rules:
            parts.append(f"layers: {layer_rules}")
        return "Prompt grammar: " + "; ".join(part for part in parts if part.split(": ", 1)[-1])

    def _visual_proof_prompt_text(self, visual_proof_spec: dict | None) -> str:
        if not visual_proof_spec:
            return ""
        must_show = ", ".join(str(item) for item in (visual_proof_spec.get("must_show") or [])[:5])
        avoid_items = list(visual_proof_spec.get("must_not_show") or [])
        avoid_items.extend(visual_proof_spec.get("semantic_fail_conditions") or [])
        avoid = ", ".join(str(item) for item in avoid_items[:4])
        mechanism = str(visual_proof_spec.get("proof_mechanism") or "the stated product benefit")
        mode = str(visual_proof_spec.get("creative_mode") or "").strip()
        scene = str(visual_proof_spec.get("scene_recipe") or visual_proof_spec.get("desired_scene") or "").strip()
        setup = f"mode {mode}; scene {scene}; " if mode or scene else ""
        return f"Visual proof: {setup}show {must_show}; prove {mechanism}; avoid {avoid}."

    def _variant_proof_style(self, variant: VariantCandidate) -> dict:
        try:
            slot = max(0, int(str(variant.variant_id).lstrip("Vv")) - 1)
        except ValueError:
            slot = 0
        styles = [
            {
                "name": "single in-use moment",
                "composition": "one scene, no split panels or symbolic icons; frame the product's point of use and the surrounding action",
                "proof_language": "product benefit is shown through body posture, placement, and natural interaction",
            },
            {
                "name": "product detail close-up",
                "composition": "bring the verified working detail into focus; angle the camera and light across its surface so its shape separates from the background",
                "proof_language": "material, component, or attachment detail carries the proof",
            },
            {
                "name": "lifestyle outcome",
                "composition": "show the product and the visible outcome together; place supporting objects where they explain how it was used",
                "proof_language": "buyer outcome is legible from the calmer/easier scene, not from labels",
            },
            {
                "name": "comparison or callout",
                "composition": "before/after, callout, arrow, checkmark, or infographic treatment is allowed",
                "proof_language": "visual annotations may clarify the mechanism if product truth stays visible",
            },
        ]
        return styles[slot % len(styles)]

    def _creative_risk_level(self, creative_specs: dict | None = None, planning: PlanningBrief | None = None) -> str:
        raw = str((creative_specs or {}).get("creative_risk_level") or "").strip().lower()
        if not raw and planning:
            raw = str((planning.creative_director_plan or {}).get("creative_risk_level") or "").strip().lower()
        aliases = {
            "low": "safe",
            "proof": "safe",
            "safe_proof": "safe",
            "medium": "social",
            "high": "bold",
            "crazy": "wildcard",
            "mad": "wildcard",
        }
        return aliases.get(raw, raw) if aliases.get(raw, raw) in {"safe", "social", "bold", "wildcard"} else "safe"

    def _creative_leap_prompt_text(self, creative_leap_spec: dict | None) -> str:
        if not creative_leap_spec:
            return ""
        device = str(creative_leap_spec.get("creative_device") or "").strip()
        metaphor = str(creative_leap_spec.get("concept_metaphor") or "").strip()
        hook = str(creative_leap_spec.get("opening_hook") or "").strip()
        reveal = str(creative_leap_spec.get("reveal_structure") or "").strip()
        must_not = ", ".join(str(item) for item in (creative_leap_spec.get("must_not_break") or [])[:4])
        plan = " / ".join(str(item) for item in (creative_leap_spec.get("shot_device_plan") or [])[:3])
        plan_part = f"; shot plan {plan}" if plan else ""
        reasoning = creative_leap_spec.get("conceptual_leap_reasoning") if isinstance(creative_leap_spec.get("conceptual_leap_reasoning"), dict) else {}
        maps = reasoning.get("why_it_maps") or {}
        map_part = f"; mapping {json.dumps(maps, ensure_ascii=False)}" if maps else ""
        rejected = reasoning.get("rejected_metaphors") or []
        reject_part = f"; rejected {', '.join(str(item) for item in rejected[:2])}" if rejected else ""
        return f"Creative leap: device {device}; metaphor {metaphor}; opening {hook}; reveal {reveal}{plan_part}{map_part}{reject_part}; do not break {must_not}."

    def _creative_leap_spec(self, variant: VariantCandidate, visual_proof_spec: dict, risk_level: str, *, is_wildcard_slot: bool = False) -> dict:
        if risk_level == "safe":
            return {}
        effective_level = "wildcard" if risk_level == "wildcard" and is_wildcard_slot else "bold" if risk_level == "wildcard" else risk_level
        proof = str(visual_proof_spec.get("proof_mechanism") or variant.message or variant.hook)
        scene = str(visual_proof_spec.get("scene_recipe") or visual_proof_spec.get("desired_scene") or variant.hook)
        selling_type = str(visual_proof_spec.get("selling_point_type") or "")
        if effective_level == "social":
            device = "micro_story"
            opening = scene
            metaphor = f"a quick real-use story that makes `{variant.angle}` obvious"
            shot_device_plan = [
                f"open inside the real use context: {scene}",
                f"show the product proof: {proof}",
                "end with the buyer outcome and product still visible",
            ]
        elif effective_level == "wildcard":
            device = "impossible_physical_metaphor"
            opening = self._wildcard_opening(variant.angle, visual_proof_spec)
            metaphor = self._wildcard_metaphor(variant.angle, visual_proof_spec)
            shot_device_plan = [
                opening,
                f"snap back to a physically plausible proof moment: {proof}",
                "hold a clear product-truth frame so the metaphor does not replace the SKU",
            ]
        else:
            device = "bait_and_reveal" if selling_type in {"functional_proof", "risk_removal"} else "visual_rhyme"
            opening = self._bold_opening(variant.angle, visual_proof_spec)
            metaphor = self._bold_metaphor(variant.angle, visual_proof_spec)
            shot_device_plan = [
                opening,
                f"reveal the product mechanism: {proof}",
                "finish with the product benefit visible in-scene",
            ]
        spec = {
            "creative_risk_level": effective_level,
            "creative_device": device,
            "concept_metaphor": metaphor,
            "opening_hook": opening,
            "reveal_structure": "start with the visual device, then reveal the product proof clearly",
            "shot_device_plan": shot_device_plan,
            "product_truth_anchor": (visual_proof_spec.get("must_show") or [])[:4],
            "must_not_break": [
                "product_truth_contract",
                "visual_proof_spec",
                "physical plausibility of the product",
                "platform/compliance constraints",
            ],
            "style_risk_level": effective_level,
            "proof_guardrail": proof,
        }
        spec["conceptual_leap_reasoning"] = self._conceptual_leap_reasoning(
            variant=variant,
            visual_proof_spec=visual_proof_spec,
            creative_leap_spec=spec,
        )
        return spec

    def _conceptual_leap_reasoning(self, *, variant: VariantCandidate, visual_proof_spec: dict, creative_leap_spec: dict) -> dict:
        proof = str(visual_proof_spec.get("proof_mechanism") or variant.message or variant.hook or variant.angle)
        must_show = [str(item) for item in (visual_proof_spec.get("must_show") or []) if str(item).strip()]
        fail_conditions = [str(item) for item in (visual_proof_spec.get("semantic_fail_conditions") or []) if str(item).strip()]
        return {
            "abstract_concept": str(variant.angle or proof or "product benefit"),
            "source_domains": ["physical analogy", "micro-story", "visual contrast"],
            "selected_metaphor": creative_leap_spec.get("concept_metaphor") or f"an unexpected but product-safe metaphor for {variant.angle}",
            "why_it_maps": {
                "product": "truth anchor",
                "proof mechanism": proof,
                "buyer outcome": "visual resolution",
            },
            "required_evidence": must_show[:4],
            "rejected_metaphors": [
                "generic lifestyle because it does not prove the benefit",
                "pure spectacle because it can replace the product truth",
                *[f"avoid semantic failure: {item}" for item in fail_conditions[:2]],
            ],
        }

    def _bold_opening(self, angle: str, visual_proof_spec: dict) -> str:
        selling_type = str(visual_proof_spec.get("selling_point_type") or "")
        if selling_type == "identity_expression":
            return "open on the identity-bearing detail being noticed before the full product context is revealed"
        if selling_type == "risk_removal":
            return "open on the problem weather or mess, then reveal the protected product in use"
        return f"open on the tension/problem that `{angle}` resolves, before showing the product"

    def _bold_metaphor(self, angle: str, visual_proof_spec: dict) -> str:
        selling_type = str(visual_proof_spec.get("selling_point_type") or "")
        if selling_type == "identity_expression":
            return "the product becomes the brand's social calling card in the scene"
        if selling_type == "risk_removal":
            return "the product turns a visible problem into a visibly safer or easier outcome"
        return f"`{angle}` is shown as a cause-and-effect visual reveal, not a generic beauty shot"

    def _wildcard_opening(self, angle: str, visual_proof_spec: dict) -> str:
        proof = str(visual_proof_spec.get("proof_mechanism") or angle)
        return f"open with an impossible-but-safe visual contradiction that makes `{proof}` immediately legible"

    def _wildcard_metaphor(self, angle: str, visual_proof_spec: dict) -> str:
        proof = str(visual_proof_spec.get("proof_mechanism") or angle)
        return f"`{proof}` becomes a surprising physical metaphor without changing product truth"

    def _variant_hook(self, angle: object) -> str:
        text = str(angle or "Show the product benefit").strip()
        text = re.sub(r"\s+", " ", text).strip(" .")
        text = re.sub(r"\s+with\s+(clear|visible)\s+product\s+proof\b", "", text, flags=re.IGNORECASE)
        if not text:
            return "Show the product benefit"
        if len(text) > 72:
            text = text[:69].rsplit(" ", 1)[0].rstrip(" ,.;:") + "..."
        return text[:1].upper() + text[1:]

    def _variant_message(self, angle: object, emotion: object) -> str:
        angle_text = str(angle or "the product benefit").strip(" .")
        emotion_text = str(emotion or "confidence").strip(" .")
        return f"Make buyers feel {emotion_text} by proving {angle_text} in the scene."

    def _planning_director_blocks(
        self,
        *,
        intake: ProductIntake,
        creative_specs: dict,
        strategic_angles: list[str],
        constraints: list[str],
    ) -> tuple[dict, dict, list[dict]]:
        product_name = intake.product_name or "the product"
        brief = "\n".join(
            item for item in [intake.manual_research_brief, str(intake.business_context.get("brief") or "")]
            if item
        )
        audience = self._normalize_text_list(
            intake.business_context.get("target_audience") or intake.business_context.get("audience") or []
        )
        visual_identity = intake.visual_identity.model_dump()
        product_truth = self._product_truth_contract(intake)
        preserve = list(dict.fromkeys(
            [str(item) for item in (product_truth.get("must_preserve") or visual_identity.get("must_preserve_details", [])) if str(item).strip()]
            or [product_name]
        ))
        media_truth = intake.asset_media_summary or visual_identity.get("raw_media_summary") or brief
        scene_hints = [
            item.strip(" -*")
            for item in re.split(r"[\n,;，；]+", f"{brief}\n{media_truth}")
            if 8 <= len(item.strip(" -*")) <= 90
            and any(word in item.lower() for word in ("scene", "setting", "bedroom", "cafe", "dinner", "office", "gallery", "wedding", "evening", "morning", "street", "sidewalk", "walk", "kitchen", "counter", "terrace", "residential"))
        ][:4]
        duration = int(creative_specs.get("video_duration_seconds") or 8)
        max_segment = self._max_video_segment_seconds(creative_specs)
        segment_count = max(1, (duration + max_segment - 1) // max_segment)
        thumb_scene = scene_hints[0] if scene_hints else f"open with {product_name} clearly visible in a specific lived-in setting"
        proof_scene = scene_hints[1] if len(scene_hints) > 1 else f"close view of {', '.join(preserve[:3])}"
        lifestyle_scene = scene_hints[2] if len(scene_hints) > 2 else "move through varied but plausible scenes from the brief or product context"
        cta_scene = scene_hints[3] if len(scene_hints) > 3 else "end on product-forward frame with a simple purchase cue"
        prompt_grammar = self._planning_prompt_grammar(
            product_name=product_name,
            brief=brief,
            media_truth=media_truth,
            creative_specs=creative_specs,
            constraints=constraints,
            product_truth=product_truth,
        )
        scene_arc = [
            {"beat": "thumb_stop", "intent": "prove the product is worth watching", "scene_direction": thumb_scene},
            {"beat": "product_truth", "intent": "lock visual identity", "scene_direction": proof_scene},
            {"beat": "lifestyle_proof", "intent": "show buyer transformation", "scene_direction": lifestyle_scene},
            {"beat": "cta", "intent": "make the next action obvious", "scene_direction": cta_scene},
        ]
        creative_director_plan = {
            "hero_insight": f"{product_name} should be sold through a concrete lifestyle moment, not generic product beauty.",
            "creative_risk_level": self._creative_risk_level(creative_specs),
            "target_audience": audience,
            "emotional_beats": ["curiosity", "recognition", "desire", "confidence"],
            "scene_arc": scene_arc,
            "must_preserve_visuals": preserve,
            "product_truth_contract": product_truth,
            "scene_hints": scene_hints,
            "do_not_show": constraints,
            "media_truth_summary": media_truth[:1200],
            "prompt_grammar": prompt_grammar,
            "source_inspiration": ["ViMax-style scene/storyboard decomposition", "OpenMontage-style proposal-to-production handoff"],
        }
        production_plan = {
            "pipeline_path": ["planning", "divergence", "video_scripting", "storyboard_image_generation", "video_generation", "visual_quality_assessment", "evaluation_selection"],
            "reference_strategy": "Use uploaded product images as storyboard/video references before historical references.",
            "segment_strategy": {
                "requested_duration_seconds": duration,
                "max_segment_seconds": max_segment,
                "estimated_segment_count": segment_count,
                "continuity_method": "extract each completed segment last frame as next segment first-frame reference",
            },
            "variant_strategy": [{"angle": angle, "director_note": f"test whether {angle} can be shown as a concrete scene, not just copy"} for angle in strategic_angles],
            "factory_risks": ["provider async latency", "product drift across segments", "storyboard too generic"],
        }
        quality_gates = [
            {"gate": "product_truth_lock", "pass_condition": f"{product_name} and preserved details remain recognizable in storyboard and every segment"},
            {"gate": "scene_specificity", "pass_condition": "each scene has place, action, emotion, and product visibility"},
            {"gate": "segment_continuity", "pass_condition": "next segment uses prior tail frame or an explicit continuity reference"},
            {"gate": "conversion_readiness", "pass_condition": "final winner has clear hook, proof, CTA, and no unsupported claims"},
        ]
        return creative_director_plan, production_plan, quality_gates

    def _download_url_bytes(self, url: str) -> bytes | None:
        try:
            with httpx.Client(timeout=90.0) as client:
                response = client.get(url)
                response.raise_for_status()
                return response.content
        except Exception:
            return None

    def _materialize_generated_image(self, generated_image) -> tuple[bytes, str]:
        b64_json = getattr(generated_image, "b64_json", None)
        if b64_json:
            try:
                return base64.b64decode(b64_json), "b64_json"
            except Exception:
                pass
        url = getattr(generated_image, "url", None)
        if url:
            content = self._download_url_bytes(url)
            if content:
                return content, "url"
        return decode_placeholder_png(), "placeholder"

    def _require_generated_image_asset(self, image_payload: dict) -> None:
        contract = self._attach_image_asset_contract(image_payload)
        flags = [str(flag) for flag in contract.get("flags") or []]
        if contract.get("blocking"):
            reason = ", ".join(flags) or str(image_payload.get("error") or "invalid_image")
            raise RuntimeError(f"image generation failed local QA: {reason}")

    def _attach_image_asset_contract(self, image_payload: dict) -> dict:
        qa = image_payload.get("visual_qa") if isinstance(image_payload.get("visual_qa"), dict) else {}
        marketplace_qa = image_payload.get("marketplace_qa") if isinstance(image_payload.get("marketplace_qa"), dict) else {}
        qa_flags = [str(flag) for flag in qa.get("flags") or []]
        marketplace_flags = [str(flag) for flag in marketplace_qa.get("flags") or []]
        source = str(image_payload.get("source") or "")
        blocking_flags = {
            "visual_qa_placeholder",
            "visual_qa_decode_error",
            "visual_qa_empty_file",
            "visual_qa_missing_file",
            "visual_qa_missing_uri",
        }
        blocking = (
            source in {"placeholder", "generation_error"}
            or qa.get("status") == "fail"
            or any(flag in blocking_flags for flag in qa_flags)
            or marketplace_qa.get("status") == "fail"
        )
        warn = qa.get("status") == "warn" or marketplace_qa.get("status") == "warn"
        contract = {
            "version": 1,
            "status": "fail" if blocking else "warn" if warn else "pass",
            "blocking": blocking,
            "recommended_action": "regenerate_image" if blocking else "manual_review" if warn else "pass_to_evaluation",
            "flags": sorted(set([*(flag.replace("visual_qa_", "media_gate_", 1) for flag in qa_flags), *marketplace_flags])),
            "qa_status": qa.get("status"),
            "marketplace_qa_status": marketplace_qa.get("status"),
        }
        image_payload["image_asset_contract"] = contract
        return contract

    def _materialize_generated_video(self, generated_video) -> tuple[bytes, str]:
        if generated_video.b64_data:
            try:
                return base64.b64decode(generated_video.b64_data), "b64_data"
            except Exception:
                pass
        if generated_video.url:
            content = self._download_url_bytes(generated_video.url)
            if content:
                return content, "url"
        return b"", "placeholder"

    def _generation_error_artifact(self, run_id: str, variant_id: str, error_text: str) -> str:
        """Write a human-readable error file when media generation fails.

        Returns the file path. Downstream visual QA will fail to parse this as
        an image, and evaluation gates will treat it as missing media.
        """
        filename = f"{variant_id}_generation_error.txt"
        content = f"GENERATION FAILED\n\n{error_text}\n"
        return self.media.write_text_artifact(run_id, filename, content)

    def _artifact_has_payload(self, uri: str | None, min_bytes: int = 1024) -> bool:
        if not uri:
            return False
        path = Path(uri)
        return path.exists() and path.is_file() and path.stat().st_size > min_bytes

    def _sample_generated_video_frames(
        self,
        *,
        run_id: str,
        variant_id: str,
        video_uri: str | None,
        generation_status: str | None,
    ) -> tuple[list[str], list[dict]]:
        status = str(generation_status or "").lower()
        if status not in {"completed", "succeeded", "success", "ready"}:
            return [], []
        if not video_uri or video_uri.startswith(("http://", "https://", "data:")):
            return [], []
        video_path = Path(video_uri)
        if not video_path.exists() or not video_path.is_file():
            return [], []
        output_dir = self.media.settings.assets_dir / run_id
        frame_uris = sample_video_frames(
            video_path=video_path,
            output_dir=output_dir,
            prefix=f"{variant_id}_generated_video",
            count=3,
        )
        frames = [
            {
                "frame_id": f"{variant_id}_generated_video_frame_{idx + 1}",
                "variant_id": variant_id,
                "uri": uri,
                "source_video_uri": video_uri,
                "frame_index": idx + 1,
            }
            for idx, uri in enumerate(frame_uris)
        ]
        return frame_uris, frames

    def _attach_generated_video_frames(self, *, run_id: str, video_payload: dict) -> dict:
        enriched = dict(video_payload or {})
        variant_id = str(enriched.get("variant_id") or "variant")
        frame_uris, frames = self._sample_generated_video_frames(
            run_id=run_id,
            variant_id=variant_id,
            video_uri=enriched.get("video_uri"),
            generation_status=enriched.get("generation_status"),
        )
        enriched["frame_uris"] = frame_uris
        enriched["generated_video_frames"] = frames
        return enriched

    def _attach_segment_frame_qa(self, *, run_id: str, segment_payload: dict) -> dict:
        if not isinstance(segment_payload, dict) or segment_payload.get("segment_frame_qa"):
            return segment_payload
        status = str(segment_payload.get("generation_status") or "").lower()
        if status not in {"completed", "succeeded", "success", "ready"} or not self._artifact_has_payload(segment_payload.get("video_uri")):
            return segment_payload
        segment_id = str(segment_payload.get("segment_id") or "segment")
        frame_uris, frames = self._sample_generated_video_frames(
            run_id=run_id,
            variant_id=segment_id,
            video_uri=segment_payload.get("video_uri"),
            generation_status=segment_payload.get("generation_status"),
        )
        qa = inspect_extracted_video_frames(frame_uris=frame_uris, social_review_contract={}, shot_plan=[])
        flags = {str(flag) for flag in qa.get("flags") or []}
        blocking = str(qa.get("status") or "") == "fail" or bool(
            flags & {"visual_qa_needs_frame_review", "visual_qa_unusable_frame_sequence"}
        )
        segment_payload["segment_frame_qa"] = {
            **qa,
            "status": "fail" if blocking else qa.get("status"),
            "blocking": blocking,
            "frame_uris": frame_uris,
            "generated_video_frames": frames,
        }
        return segment_payload

    def _merge_video_frame_review(
        self,
        *,
        qa: dict[str, object],
        frame_review: dict[str, object],
    ) -> dict[str, object]:
        checks = [*(qa.get("checks") or []), *(frame_review.get("checks") or [])]
        flags = sorted({str(flag) for flag in [*(qa.get("flags") or []), *(frame_review.get("flags") or [])]})
        qa_score = float(qa.get("score") or 0.0)
        frame_score = float(frame_review.get("score") or qa_score)
        fail_count = sum(1 for check in checks if isinstance(check, dict) and check.get("status") == "fail")
        warn_count = sum(
            1 for check in checks if isinstance(check, dict) and check.get("status") in {"warn", "manual_review"}
        )
        status = "fail" if fail_count else "warn" if warn_count else "pass"
        metrics = dict(qa.get("metrics") or {})
        metrics["frame_review"] = {
            "frame_count": frame_review.get("frame_count"),
            "first_frame_uri": frame_review.get("first_frame_uri"),
        }
        return {
            **qa,
            "status": status,
            "score": round(min(qa_score, frame_score), 2),
            "flags": flags,
            "checks": checks,
            "metrics": metrics,
        }

    def _normalize_text_list(self, value: object) -> list[str]:
        if isinstance(value, str):
            trimmed = value.strip()
            return [trimmed] if trimmed else []
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _video_product_context(
        self,
        *,
        intake: ProductIntake | None,
        business_context: dict | None,
        creative_specs: dict | None,
    ) -> dict:
        business_context = business_context or {}
        creative_specs = creative_specs or {}
        product_name = (intake.product_name if intake and intake.product_name else "") or str(
            business_context.get("product_name") or "the product"
        )
        audience_list = self._normalize_text_list(
            business_context.get("target_audience") or business_context.get("audience") or []
        )
        product_truth = self._product_truth_contract(intake)
        return {
            "product_name": product_name,
            "audience": ", ".join(audience_list[:3]) if audience_list else "target buyers",
            "value_props": self._normalize_text_list(
                business_context.get("key_value_props") or business_context.get("value_props") or []
            ),
            "primary_cta": str(business_context.get("primary_cta") or "Shop Now"),
            "media_summary": intake.asset_media_summary if intake and intake.asset_media_summary else "",
            "platform": str(creative_specs.get("platform") or ""),
            "creative_goal": str(creative_specs.get("creative_goal") or ""),
            "product_truth_contract": product_truth,
        }

    def _video_generation_spec(self, creative_specs: dict | None) -> dict:
        creative_specs = creative_specs or {}
        spec: dict[str, object] = {
            "size": self._normalize_media_size(str(creative_specs.get("video_size") or creative_specs.get("image_size") or "9:16")),
            "resolution": str(creative_specs.get("resolution") or "720p"),
            "duration": int(creative_specs.get("video_duration_seconds") or 8),
        }
        if creative_specs.get("video_image_urls") and not creative_specs.get("image_urls"):
            spec["image_urls"] = creative_specs.get("video_image_urls")
        for key in (
            "generate_audio",
            "return_last_frame",
            "seed",
            "tools",
            "image_urls",
            "image_with_roles",
            "video_urls",
            "audio_urls",
            "max_reference_images",
        ):
            if key in creative_specs and creative_specs.get(key) not in (None, "", []):
                spec[key] = creative_specs.get(key)
        return spec

    def _normalize_media_size(self, size: str) -> str:
        normalized = (size or "").strip().lower()
        return {
            "720x1280": "9:16",
            "1080x1920": "9:16",
            "1280x720": "16:9",
            "1920x1080": "16:9",
            "1024x1024": "1:1",
        }.get(normalized, size or "9:16")

    def _max_video_segment_seconds(self, creative_specs: dict | None) -> int:
        try:
            requested = int((creative_specs or {}).get("max_video_segment_seconds") or 15)
        except (TypeError, ValueError):
            requested = 15
        return max(4, min(15, requested))

    def _segment_durations(self, total_seconds: int, max_seconds: int) -> list[int]:
        if total_seconds <= max_seconds:
            return [total_seconds]
        segment_count = (total_seconds + max_seconds - 1) // max_seconds
        durations: list[int] = []
        remaining = total_seconds
        for idx in range(segment_count):
            slots = segment_count - idx
            duration = (remaining + slots - 1) // slots
            durations.append(duration)
            remaining -= duration
        return durations

    def _long_video_segment_role(self, idx: int, count: int) -> dict[str, str]:
        if idx == 0:
            return {
                "intent": "thumb_stop",
                "role": "opening hook",
                "instruction": "open the one continuous story; do not resolve the ad yet",
            }
        if idx == count - 1:
            return {
                "intent": "cta_packshot",
                "role": "desire and CTA",
                "instruction": "pay off the prior proof and finish with one concise CTA",
            }
        return {
            "intent": "product_proof",
            "role": "proof escalation",
            "instruction": "continue from the prior moment with new proof; do not restart the hook",
        }

    def _sequence_long_video_segments(self, segments: list[VideoSegmentPlan], product_name: str) -> list[VideoSegmentPlan]:
        if len(segments) <= 1:
            return segments
        sequenced: list[VideoSegmentPlan] = []
        for idx, segment in enumerate(segments):
            role = self._long_video_segment_role(idx, len(segments))
            constraints = list(segment.continuity_constraints or [])
            for item in [
                "single_continuous_video_no_restart",
                "preserve_same_model_product_and_mood",
                f"keep {product_name} visually consistent",
            ]:
                if item not in constraints:
                    constraints.append(item)
            if idx > 0 and "start_from_previous_tail_frame" not in constraints:
                constraints.append("start_from_previous_tail_frame")
            if idx < len(segments) - 1 and "end_on_bridgeable_action_for_next_segment" not in constraints:
                constraints.append("end_on_bridgeable_action_for_next_segment")
            original_motion = segment.motion_prompt or segment.first_frame_prompt or segment.scene
            segment.scene = f"{role['role']}: {segment.scene or role['intent']}"
            segment.shot_intent = role["intent"]
            segment.motion_prompt = (
                f"Segment {idx + 1}/{len(segments)} of one continuous ad: {role['instruction']}. "
                f"{original_motion}"
            )
            if idx > 0:
                segment.first_frame_prompt = (
                    "Continue directly from the previous segment tail frame; do not introduce a new TikTok opening. "
                    f"{segment.first_frame_prompt}"
                )
            if idx < len(segments) - 1:
                segment.last_frame_prompt = (
                    "End with a bridgeable action or camera move that the next segment can continue. "
                    f"{segment.last_frame_prompt}"
                )
                segment.transition_to_next = "match_cut"
            else:
                segment.transition_to_next = "none"
            risk_instruction = self._human_motion_risk_instruction(" ".join([segment.scene, segment.motion_prompt, segment.first_frame_prompt]))
            if risk_instruction and "avoid_high_risk_human_motion" not in constraints:
                constraints.append("avoid_high_risk_human_motion")
            segment.continuity_constraints = constraints
            sequenced.append(segment)
        return sequenced

    def _apply_segment_contracts(
        self,
        segments: list[VideoSegmentPlan],
        *,
        product_truth: dict,
        product_name: str,
    ) -> list[VideoSegmentPlan]:
        if not segments:
            return segments
        must_preserve = list(product_truth.get("must_preserve") or [product_name])
        for idx, segment in enumerate(segments):
            contract = dict(segment.segment_contract or {})
            contract.update(
                {
                    "version": 1,
                    "segment_id": segment.segment_id,
                    "segment_index": idx,
                    "segment_count": len(segments),
                    "product_name": product_name,
                    "must_preserve": must_preserve[:12],
                    "colors": list(product_truth.get("colors") or []),
                    "materials": list(product_truth.get("materials") or []),
                    "forbidden_changes": list(product_truth.get("forbidden_changes") or []),
                    "continuity_constraints": list(segment.continuity_constraints or []),
                    "first_frame_prompt": segment.first_frame_prompt,
                    "last_frame_prompt": segment.last_frame_prompt,
                    "transition_to_next": segment.transition_to_next,
                    "preflight_checks": [
                        "product_truth_visible_in_first_frame",
                        "same_product_identity_across_segment",
                        "no_added_or_removed_core_product_parts",
                        "bridgeable_tail_frame" if idx < len(segments) - 1 else "final_product_readable",
                    ],
                }
            )
            segment.segment_contract = contract
        return segments

    def _stitch_preflight(self, *, segments: list[VideoSegmentPlan], segment_payloads: list[dict]) -> dict:
        checks: list[dict] = []
        pending = False
        if len(segment_payloads) != len(segments):
            checks.append({"key": "segment_count", "status": "warn", "message": "Not all segment payloads are available yet."})
            pending = True
        for idx, segment in enumerate(segments):
            payload = segment_payloads[idx] if idx < len(segment_payloads) else {}
            status = str(payload.get("generation_status") or "").lower()
            if status in {"", "submitted", "queued", "pending", "processing", "running", "missing"}:
                checks.append({"key": f"{segment.segment_id}.status", "status": "warn", "message": f"Segment is {status or 'missing'}."})
                pending = True
            elif status not in {"completed", "succeeded", "success", "ready"}:
                checks.append({"key": f"{segment.segment_id}.status", "status": "fail", "message": f"Segment is {status or 'missing'}."})
            if not payload.get("segment_contract") and not segment.segment_contract:
                checks.append({"key": f"{segment.segment_id}.segment_contract", "status": "fail", "message": "Segment contract is missing."})
            frame_qa = payload.get("segment_frame_qa") if isinstance(payload.get("segment_frame_qa"), dict) else {}
            if status in {"completed", "succeeded", "success", "ready"}:
                if not frame_qa:
                    checks.append({"key": f"{segment.segment_id}.segment_frame_qa", "status": "fail", "message": "Segment frame QA has not run."})
                elif frame_qa.get("blocking") or str(frame_qa.get("status") or "").lower() == "fail":
                    checks.append(
                        {
                            "key": f"{segment.segment_id}.segment_frame_qa",
                            "status": "fail",
                            "message": "Segment frame QA blocked stitching.",
                            "flags": frame_qa.get("flags") or [],
                        }
                    )
            if idx < len(segments) - 1 and not payload.get("last_frame_uri") and not payload.get("last_frame_url"):
                if status in {"completed", "succeeded", "success", "ready"}:
                    checks.append({"key": f"{segment.segment_id}.tail_frame", "status": "fail", "message": "Bridge tail frame is missing."})
                else:
                    checks.append({"key": f"{segment.segment_id}.tail_frame", "status": "warn", "message": "Bridge tail frame is not available yet."})
                    pending = True
        if not checks:
            checks.append({"key": "stitch_preflight", "status": "pass", "message": "All completed segments are stitch-ready."})
        failed = any(check["status"] == "fail" for check in checks)
        return {
            "status": "fail" if failed else "pending" if pending else "pass",
            "checks": checks,
            "flags": ["stitch_preflight_failed"] if failed else [],
        }

    def _segment_ledger(
        self,
        *,
        variant_id: str,
        segment_queue: list[dict],
        segment_payloads: list[dict],
        stitch_preflight: dict | None = None,
    ) -> dict:
        stitch_preflight = stitch_preflight or {}
        payload_by_id = {str(item.get("segment_id")): item for item in segment_payloads if isinstance(item, dict)}
        failed_keys = {
            str(check.get("key") or "")
            for check in (stitch_preflight.get("checks") or [])
            if isinstance(check, dict) and check.get("status") == "fail"
        }
        rows: list[dict] = []
        first_blocked_segment_id = None
        for idx, queued in enumerate(segment_queue):
            segment_id = str(queued.get("segment_id") or f"{variant_id}_S{idx + 1}")
            payload = payload_by_id.get(segment_id) or {}
            status = str(payload.get("generation_status") or ("queued" if idx >= len(segment_payloads) else "pending")).lower()
            contract = payload.get("segment_contract") or queued.get("segment_contract") or {}
            reference_manifest = payload.get("reference_manifest") if isinstance(payload.get("reference_manifest"), list) else []
            segment_failed = any(key.startswith(f"{segment_id}.") for key in failed_keys)
            ledger_status = "needs_regeneration" if segment_failed else status
            if segment_failed and not first_blocked_segment_id:
                first_blocked_segment_id = segment_id
            rows.append(
                {
                    "segment_id": segment_id,
                    "segment_index": idx,
                    "status": ledger_status,
                    "generation_status": payload.get("generation_status"),
                    "contract": contract,
                    "reference_mode": payload.get("reference_mode"),
                    "reference_image_count": payload.get("reference_image_count", 0),
                    "reference_manifest": reference_manifest,
                    "hosted_reference_ready": all(
                        bool(item.get("provider_usable")) for item in reference_manifest if isinstance(item, dict) and item.get("used")
                    ),
                    "segment_frame_qa_status": (payload.get("segment_frame_qa") or {}).get("status") if isinstance(payload.get("segment_frame_qa"), dict) else None,
                    "segment_frame_qa_flags": (payload.get("segment_frame_qa") or {}).get("flags", []) if isinstance(payload.get("segment_frame_qa"), dict) else [],
                    "video_uri": payload.get("video_uri"),
                    "tail_frame": payload.get("last_frame_url") or payload.get("last_frame_uri"),
                    "qa_status": (payload.get("visual_qa") or {}).get("status") if isinstance(payload.get("visual_qa"), dict) else None,
                    "preflight_failed": segment_failed,
                }
            )
        if stitch_preflight.get("status") == "fail":
            status = "needs_regeneration"
        elif rows and all(row["status"] in {"completed", "succeeded", "success", "ready"} for row in rows):
            status = "completed"
        elif any(row["status"] in {"processing", "pending", "queued", "submitted", "running"} for row in rows):
            status = "pending"
        else:
            status = "pending" if rows else "empty"
        return {
            "version": 1,
            "variant_id": variant_id,
            "status": status,
            "first_blocked_segment_id": first_blocked_segment_id,
            "segments": rows,
            "stitch_preflight_status": stitch_preflight.get("status"),
        }

    def _segment_resume_state(self, *, segments: list[VideoSegmentPlan], resume_payload: dict | None) -> tuple[int, list[dict], list[Path], str | None]:
        if not segments or not isinstance(resume_payload, dict) or not resume_payload:
            return 0, [], [], None
        ledger = resume_payload.get("segment_ledger") or {}
        blocked_id = ledger.get("first_blocked_segment_id")
        if not blocked_id:
            for row in ledger.get("segments") or []:
                if isinstance(row, dict) and row.get("status") in {"needs_regeneration", "failed", "error"}:
                    blocked_id = row.get("segment_id")
                    break
        if not blocked_id:
            return 0, [], [], None
        resume_index = next((idx for idx, segment in enumerate(segments) if segment.segment_id == blocked_id), 0)
        existing_by_id = {
            str(item.get("segment_id")): dict(item)
            for item in (resume_payload.get("segments") or [])
            if isinstance(item, dict)
        }
        preserved: list[dict] = []
        completed_paths: list[Path] = []
        bridge_frame_uri = None
        for segment in segments[:resume_index]:
            payload = existing_by_id.get(segment.segment_id)
            if not payload:
                break
            status = str(payload.get("generation_status") or "").lower()
            if status not in {"completed", "succeeded", "success", "ready"} or not self._artifact_has_payload(payload.get("video_uri")):
                break
            preserved.append(payload)
            completed_paths.append(Path(str(payload["video_uri"])))
            bridge_frame_uri = payload.get("last_frame_url") or payload.get("last_frame_uri")
        return len(preserved), preserved, completed_paths, bridge_frame_uri

    def _build_video_segments(
        self,
        *,
        variant_id: str,
        shot_plan: list[ShotPlanItem],
        shot_list: list[str],
        total_seconds: int,
        creative_specs: dict | None,
        product_name: str,
        product_truth: dict | None = None,
    ) -> list[VideoSegmentPlan]:
        durations = self._segment_durations(total_seconds, self._max_video_segment_seconds(creative_specs))
        if len(durations) == 1:
            return []
        source_shots = shot_plan or [
            ShotPlanItem(
                shot_id=f"shot_{idx + 1}",
                variant_id=variant_id,
                intent="product_demo",
                first_frame=ShotFramePlan(description=shot, visible_product_elements=[product_name]),
                motion_description=shot,
            )
            for idx, shot in enumerate(shot_list or [f"{product_name} product demo"])
        ]
        segments: list[VideoSegmentPlan] = []
        for idx, duration in enumerate(durations):
            shot = source_shots[min(idx, len(source_shots) - 1)]
            last_frame = shot.last_frame or shot.first_frame
            segments.append(
                VideoSegmentPlan(
                    segment_id=f"{variant_id}_S{idx + 1}",
                    variant_id=variant_id,
                    duration_seconds=float(duration),
                    scene=shot.intent,
                    shot_intent=shot.intent,
                    first_frame_prompt=shot.first_frame.description,
                    last_frame_prompt=last_frame.description,
                    motion_prompt=shot.motion_description or shot.first_frame.description,
                    transition_to_next="none" if idx == len(durations) - 1 else "match_cut",
                    variation_type="medium" if shot.last_frame else "small",
                    continuity_constraints=shot.product_continuity_constraints,
                )
            )
        segments = self._sequence_long_video_segments(segments, product_name)
        return self._apply_segment_contracts(
            segments,
            product_truth=product_truth or {},
            product_name=product_name,
        )

    def _video_prompt_quality_block(self, product_context: dict | None) -> str:
        product_context = product_context or {}
        product_name = str(product_context.get("product_name") or "the product")
        director_handoff = dict(product_context.get("director_plan") or {})
        director_plan = dict(director_handoff.get("creative_director_plan") or {})
        prompt_grammar = dict(product_context.get("prompt_grammar") or director_plan.get("prompt_grammar") or {})
        grammar_line = self._prompt_grammar_text(prompt_grammar)
        return (
            f"Keep {product_name} visually consistent with the submitted product context. "
            "Preserve recognizable form, materials, proportions, attachment logic, and key functional details. "
            "Do not invent extra components, hide critical product details, or produce physically implausible interactions."
            f" {grammar_line}"
        )

    def _business_strategy_system_prompt(self, agent_role: str) -> str:
        return (
            f"You are acting as {agent_role} in a commercial advertising creative pipeline. "
            "Operate like a senior growth strategist, not a generic copywriter. Preserve product truths, "
            "state assumptions, separate commercial hypothesis from compliance-sensitive claims, and produce "
            "handoff-ready decisions that another agent can audit. Never hide uncertainty."
        )

    def _persona_contract_prompt(self, runtime_config: dict | None) -> str:
        compiled_persona = (runtime_config or {}).get("compiled_persona")
        return render_persona_prompt(compiled_persona)

    def _qa_repair_prompt_block(self, runtime_config: dict | None) -> str:
        repair = (runtime_config or {}).get("qa_repair")
        if isinstance(repair, dict):
            prompt = str(repair.get("prompt") or "").strip()
        else:
            prompt = str((runtime_config or {}).get("qa_repair_prompt") or "").strip()
        return f"\n\n{prompt}" if prompt else ""

    def _variant_visual_proof_spec(self, variant: VariantCandidate, scene_direction: str = "", context_text: str = "") -> dict:
        existing = dict(variant.visual_proof_spec or {})
        if existing:
            return existing
        source_text = " ".join([variant.angle, variant.hook, variant.message, scene_direction, context_text]).strip()
        recipe = self._visual_proof_recipe(source_text)
        spec = {
            "selling_point_type": recipe["selling_point_type"],
            "creative_mode": recipe["creative_mode"],
            "proof_style": self._variant_proof_style(variant),
            "desired_scene": scene_direction or recipe["scene_recipe"] or variant.hook or variant.angle,
            "scene_recipe": recipe["scene_recipe"],
            "proof_mechanism": recipe["proof_mechanism"] or variant.message or variant.hook,
            "must_show": list(dict.fromkeys([*[item for item in [variant.angle, variant.hook] if item], *recipe["must_show"]])),
            "must_not_show": list(dict.fromkeys(["generic lifestyle scene without visible product proof", *recipe["must_not_show"]])),
            "semantic_fail_conditions": list(dict.fromkeys(["visual does not prove the stated product benefit", *recipe["semantic_fail_conditions"]])),
        }
        return spec

    def _visual_proof_recipe(self, source_text: str) -> dict[str, list[str] | str]:
        text = source_text.lower()
        if re.search(r"\b(protect|prevent|avoid|safe|secure|risk|weather|waterproof|dry|control|controlled|redirect|stabilize|reduce)\b|防|保护|避免|稳定|控制", text):
            return {
                "selling_point_type": "risk_removal",
                "creative_mode": "proof_demo",
                "scene_recipe": "",
                "proof_mechanism": "",
                "must_show": ["the risk or problem", "the product visibly addressing it", "the safer or more controlled outcome"],
                "must_not_show": ["the problem continuing unchanged", "the product hidden during the proof"],
                "semantic_fail_conditions": ["visual implies the product failed to reduce the stated risk or problem"],
            }
        if re.search(r"\b(custom|brand|identity|logo|personal|personalized|print|design|signature)\b|定制|品牌|个性|印刷", text):
            return {
                "selling_point_type": "identity_expression",
                "creative_mode": "creative_scene",
                "scene_recipe": "",
                "proof_mechanism": "",
                "must_show": ["the identity-bearing detail", "the product clearly carrying that identity", "a context where the identity matters"],
                "must_not_show": ["generic product with no identity cue", "invented branding that overrides the submitted product truth"],
                "semantic_fail_conditions": ["visual hides the stated identity or customization benefit"],
            }
        if re.search(r"\b(clean|before|after|transform|restore|improve|remove|reveal|repair|upgrade)\b|清洁|前后|改善|修复|去除", text):
            return {
                "selling_point_type": "functional_proof",
                "creative_mode": "proof_demo",
                "scene_recipe": "",
                "proof_mechanism": "",
                "must_show": ["the starting state", "the product causing the change", "the visible result"],
                "must_not_show": ["result appears without product action", "product category changes during the proof"],
                "semantic_fail_conditions": ["visual shows the product but no evidence of the stated transformation"],
            }
        return {
            "selling_point_type": "general_product_benefit",
            "creative_mode": "creative_scene",
            "scene_recipe": "",
            "proof_mechanism": "",
            "must_show": [],
            "must_not_show": [],
            "semantic_fail_conditions": [],
        }

    def _variant_visual_proof_prompt_block(self, variant: VariantCandidate) -> str:
        spec = self._variant_visual_proof_spec(variant)
        if not spec:
            return ""
        leap = self._creative_leap_prompt_text(variant.creative_leap_spec)
        proof_style = spec.get("proof_style") if isinstance(spec.get("proof_style"), dict) else {}
        return (
            f"Variant visual proof spec: {json.dumps(spec, ensure_ascii=False)}. "
            + (f"{leap} " if leap else "")
            + f"Use proof style `{proof_style.get('name', 'variant-specific composition')}`: "
            + f"{proof_style.get('composition', 'make the composition distinct from sibling variants')}. "
            + f"Proof language: {proof_style.get('proof_language', 'show the benefit through the scene')}. "
            + "Make the image prove this specific visual mechanism, not just the broad product category. "
        )

    def _variant_visual_proof_specs(self, variant_set: VariantSet) -> dict[str, dict]:
        return {
            variant.variant_id: self._variant_visual_proof_spec(variant)
            for variant in variant_set.variants
        }

    def _variant_creative_leap_specs(self, variant_set: VariantSet) -> dict[str, dict]:
        return {
            variant.variant_id: dict(variant.creative_leap_spec or {})
            for variant in variant_set.variants
            if variant.creative_leap_spec
        }

    def _apply_visual_proof_reviews(self, *, reports: list[dict], summaries: list[dict], model_summary: str) -> str:
        parsed = self._parse_llm_json(model_summary, schema_key="visual_proof_reviews")
        reviews = parsed.get("visual_proof_reviews") or []
        if not isinstance(reviews, list):
            raise ValueError("visual_proof_reviews must be a list")
        by_variant = {
            str(item.get("variant_id") or ""): item
            for item in reviews
            if isinstance(item, dict) and str(item.get("variant_id") or "").strip()
        }
        missing_scores = [
            str(report.get("variant_id") or "")
            for report in reports
            if not isinstance((by_variant.get(str(report.get("variant_id") or "")) or {}).get("visual_score"), (int, float))
        ]
        if missing_scores:
            raise ValueError(f"visual_score missing for variants: {', '.join(missing_scores)}")
        for report in reports:
            review = by_variant.get(str(report.get("variant_id") or ""))
            if not review:
                continue
            status = str(review.get("status") or review.get("visual_proof_status") or "").lower()
            report["visual_proof_qa"] = review
            if isinstance(review.get("visual_score"), (int, float)):
                report["visual_score"] = round(float(review["visual_score"]), 2)
            if status == "fail":
                issues = set(report.get("blocking_issues") or [])
                issues.add("visual_qa_visual_proof_failed")
                for condition in review.get("failed_conditions") or []:
                    if str(condition).strip():
                        issues.add(f"visual_proof_failed:{str(condition).strip()[:120]}")
                report["blocking_issues"] = sorted(issues)
                report["qa_status"] = "fail"
                report["recommended_action"] = "request_regeneration"
        for summary in summaries:
            review = by_variant.get(str(summary.get("variant_id") or ""))
            if not review:
                continue
            status = str(review.get("status") or review.get("visual_proof_status") or "").lower()
            summary["visual_proof_qa"] = review
            if isinstance(review.get("visual_score"), (int, float)):
                summary["visual_score"] = round(float(review["visual_score"]), 2)
            if status == "fail":
                issues = set(summary.get("issues") or [])
                issues.add("visual_qa_visual_proof_failed")
                for condition in review.get("failed_conditions") or []:
                    if str(condition).strip():
                        issues.add(f"visual_proof_failed:{str(condition).strip()[:120]}")
                summary["issues"] = sorted(issues)
                summary["qa_status"] = "fail"
                summary["recommended_action"] = "request_regeneration"
                summary["blocking_issue_count"] = max(int(summary.get("blocking_issue_count") or 0), len(issues))
        return str(parsed.get("model_summary") or model_summary)

    def _mark_visual_model_review_unavailable(
        self,
        *,
        reports: list[dict],
        summaries: list[dict],
    ) -> None:
        for report in reports:
            report["model_review_status"] = "unavailable"
            report["visual_score"] = None
            if report.get("qa_status") == "pass":
                report["qa_status"] = "warn"
                report["recommended_action"] = "manual_review"
                report["review_notes"] = f"{report.get('variant_id')} visual QA warn; model review unavailable."
        for summary in summaries:
            summary["visual_score"] = None
            if summary.get("qa_status") == "pass":
                summary["qa_status"] = "warn"
                summary["recommended_action"] = "manual_review"
            issues = list(summary.get("issues") or [])
            issues.append("visual_qa_model_review_unavailable")
            summary["issues"] = sorted(set(issues))

    def _compose_stage_prompt(
        self,
        *,
        runtime_config: dict | None,
        task_instruction: str,
        agent_role: str | None = None,
    ) -> str:
        blocks: list[str] = []
        persona_prompt = self._persona_contract_prompt(runtime_config)
        if persona_prompt:
            blocks.append(persona_prompt)
        if agent_role:
            blocks.append(self._business_strategy_system_prompt(agent_role))
        blocks.append(task_instruction)
        return "\n\n".join(block for block in blocks if block).strip()

    def _parse_llm_json(self, response_text: str, schema_key: str) -> dict:
        """Parse JSON from an LLM response, stripping markdown code fences.

        Returns the full parsed dict on success.  Raises ValueError when
        ``response_text`` is empty, the JSON is invalid, or *schema_key*
        is missing from the parsed dict.
        """
        if not isinstance(response_text, str) or not response_text.strip():
            raise ValueError("LLM response text is empty")
        text = response_text.strip()
        # Strip ```json ... ``` fences
        fence_match = re.match(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse LLM JSON response: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM response is not a JSON object (got {type(parsed).__name__})")
        if schema_key not in parsed:
            raise ValueError(f"Expected key {schema_key!r} not found in LLM JSON response")
        return parsed

    def _business_strategy_handoff(self, *, stage: str, decisions: list[str], risks: list[str], review_questions: list[str]) -> dict:
        return {
            "stage": stage,
            "decisions": decisions,
            "risks": risks,
            "review_questions": review_questions,
            "handoff_standard": "commercial-pilot-v2",
        }

    def _dtc_site_surface_strategy(self, creative_specs: dict | None) -> dict:
        return get_dtc_site_surface_strategy(creative_specs)

    def _dtc_surface_prompt_block(self, creative_specs: dict | None) -> str:
        strategy = self._dtc_site_surface_strategy(creative_specs)
        if not strategy:
            return ""
        return (
            f"This asset is for the DTC website {strategy['display_name']} surface. "
            f"Composition focus: {strategy['composition_focus']}. "
            f"Framing guidance: {strategy['framing_guidance']} "
            f"Negative space rule: {strategy['negative_space_policy']}. "
            f"Product visibility rule: {strategy['product_visibility_rule']} "
            f"Backdrop style: {strategy['backdrop_style']}. "
            f"Avoid: {', '.join(strategy['forbidden_elements'])}. "
        )

    def _local_media_qa(self, *, asset_type: str, uri: str | None, payload: dict | None = None, expected_ratio: str | None = None) -> dict:
        return inspect_visual_asset(asset_type=asset_type, uri=uri, payload=payload or {}, expected_ratio=expected_ratio)

    def run_intake(
        self,
        run_id: str,
        payload: dict,
        *,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
    ) -> StageOutput:
        intake = ProductIntake(
            product_name=payload.get("product_name", "unknown_product"),
            market=payload.get("market", "US"),
            locale=payload.get("locale", "en-US"),
            category_tags=payload.get("category_tags", []),
            business_context=payload.get("business_context", {}),
            manual_research_brief=payload.get("manual_research_brief", ""),
            url_references=payload.get("context", {}).get("url_references", []),
            sku_summary=payload.get("context", {}).get("input_assets", {}).get("sku_summary", []),
            image_references=payload.get("context", {}).get("input_assets", {}).get("sample_images", []),
            video_references=payload.get("context", {}).get("input_assets", {}).get("sample_videos", []),
        )
        estimated_cost = 0.0
        model_used = model
        image_inputs = self._reference_image_inputs(intake)
        video_inputs = self._reference_video_inputs(intake)
        media_summary = ""
        if image_inputs or video_inputs:
            media_prompt = (
                "Analyze uploaded product media and output concise ad-useful facts: "
                "product appearance, material clues, fit/wearing method, usage scenes, motion cues, "
                "do-not-change constraints, and quality/compliance cautions."
            )
            try:
                media_summary, media_model_used, media_cost = self._chat_complete(
                    provider,
                    model,
                    media_prompt,
                    runtime_config,
                    image_urls=image_inputs,
                    video_urls=video_inputs,
                )
                model_used = media_model_used
                estimated_cost += media_cost
            except Exception as exc:
                if image_inputs:
                    try:
                        media_summary, media_model_used, media_cost = self._chat_complete(
                            provider,
                            model,
                            media_prompt,
                            runtime_config,
                            image_urls=image_inputs,
                        )
                        model_used = media_model_used
                        estimated_cost += media_cost
                    except Exception as image_exc:
                        media_summary = f"media_analysis_failed: {exc}; image_fallback_failed: {image_exc}"
                else:
                    media_summary = f"media_analysis_failed: {exc}"

        prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="GM Orchestrator",
            task_instruction=(
                "Normalize this product intake for ad creative generation in concise, execution-ready bullets. "
                f"product_name={intake.product_name}; market={intake.market}; locale={intake.locale}; "
                f"category_tags={intake.category_tags}; business_context={intake.business_context}; "
                f"manual_research_brief={intake.manual_research_brief}; "
                f"uploaded_assets={{'sku_count': {len(intake.sku_summary)}, 'image_count': {len(intake.image_references)}, "
                f"'video_count': {len(intake.video_references)}}}; asset_media_summary={media_summary[:1200]}"
            ),
        )
        summary, model_used, llm_cost = self._chat_complete(provider, model, prompt, runtime_config)
        estimated_cost += llm_cost
        intake.asset_media_summary = media_summary
        intake.visual_identity = ProductVisualIdentity.model_validate(
            infer_visual_identity(
                product_name=intake.product_name,
                category_tags=intake.category_tags,
                media_summary=media_summary,
                image_references=intake.image_references,
                video_references=intake.video_references,
            )
        )
        intake.product_truth_contract = build_product_truth_contract(
            product_name=intake.product_name,
            visual_identity=intake.visual_identity.model_dump(),
            sku_summary=intake.sku_summary,
        )
        normalized = {**intake.model_dump(), "llm_summary": summary}
        uri = self.media.write_text_artifact(run_id, "intake_summary.json", intake.model_dump_json(indent=2))
        return StageOutput(
            payload=normalized,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=[{"type": "intake_summary", "uri": uri, "payload": normalized}],
        )

    def run_planning(
        self,
        run_id: str,
        intake: ProductIntake,
        *,
        gm_lessons: list[dict],
        research_context: dict | None = None,
        gm_policy: dict | None = None,
        creative_specs: dict | None = None,
        enable_research: bool,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
    ) -> StageOutput:
        mode = "online_research_enabled" if enable_research else "manual_research_only"
        gm_policy = gm_policy or {}
        research_context = research_context or {}
        creative_specs = creative_specs or {}
        policy_excerpt = gm_policy.get("stage_guidance") or {}
        surface_strategy = self._dtc_site_surface_strategy(creative_specs)
        prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="Planning Agent",
            task_instruction=(
                f"Build planning brief in {mode}. intake={intake.model_dump()} "
                f"gm_lessons={gm_lessons[:3]}. research_context={research_context}. "
                f"Use only included research_context entries; treat excluded_research as unavailable unless pinned/reviewed. "
                f"gm_policy={policy_excerpt}. dtc_surface_strategy={surface_strategy}. Return concise strategy, constraints, hypotheses, risk boundaries, "
                "creative director plan, scene arc, reference strategy, segment continuity plan, quality gates, and reviewer decision questions."
            ),
        )
        summary, model_used, estimated_cost = self._chat_complete(provider, model, prompt, runtime_config)
        value_props = intake.business_context.get("key_value_props", [])
        memory_angles = _gm_lesson_angle_priorities(gm_lessons)
        strategic_angles = (gm_policy.get("angle_priorities") or [])[:3] or memory_angles[:3] or value_props[:3] or [
            "time-saving daily workflow",
            "visible before/after proof",
            "risk-free practical messaging",
        ]
        constraints = list(intake.business_context.get("prohibited_claims", []))
        constraints.extend(str(item) for item in (gm_policy.get("hard_constraints") or [])[:5])
        if surface_strategy:
            constraints.append(
                f"{surface_strategy['display_name']}: {surface_strategy['product_visibility_rule']}"
            )
            constraints.append(
                f"{surface_strategy['display_name']}: {surface_strategy['negative_space_policy']}"
            )
        constraints = list(dict.fromkeys(item for item in constraints if str(item).strip()))
        shop_thesis = gm_policy.get("shop_thesis") or {}
        creative_director_plan, production_plan, quality_gates = self._planning_director_blocks(
            intake=intake,
            creative_specs=creative_specs,
            strategic_angles=strategic_angles,
            constraints=constraints,
        )
        planning = PlanningBrief(
            strategic_angles=strategic_angles,
            audience_priorities=self._normalize_text_list(intake.business_context.get("target_audience") or intake.business_context.get("audience") or []),
            positioning=shop_thesis.get("positioning") or intake.business_context.get("positioning", ""),
            constraints=constraints,
            gm_lessons=gm_lessons[:5],
            surface_strategy=surface_strategy,
            creative_director_plan=creative_director_plan,
            production_plan=production_plan,
            quality_gates=quality_gates,
        )
        strategy_handoff = self._business_strategy_handoff(
            stage="planning",
            decisions=[
                f"positioning={planning.positioning}",
                f"primary_audience={planning.audience_priorities[0] if planning.audience_priorities else 'general'}",
                f"angle_count={len(planning.strategic_angles)}",
                f"site_surface={surface_strategy.get('site_surface') or 'none'}",
                f"segment_count={production_plan['segment_strategy']['estimated_segment_count']}",
            ],
            risks=[str(item) for item in constraints] or ["No explicit prohibited claims supplied."],
            review_questions=[
                "Are the listed product truths sufficient for generation?",
                "Should any claim boundary be tightened before variants are generated?",
                "Which angle should be deprioritized for this market or channel?",
            ],
        )
        output = {
            **planning.model_dump(),
            "planning_mode": mode,
            "llm_summary": summary,
            "strategy_handoff": strategy_handoff,
            "surface_strategy": surface_strategy,
            "commercial_strategy": {
                "audience": planning.audience_priorities,
                "positioning": planning.positioning,
                "angle_portfolio": planning.strategic_angles,
                "claim_boundaries": planning.constraints,
                "creative_director_plan": creative_director_plan,
                "production_plan": production_plan,
                "quality_gates": quality_gates,
                "memory_applied_count": len(gm_lessons[:5]),
                "surface_strategy": surface_strategy,
                "active_gm_policy": {
                    "policy_version_ids": gm_policy.get("policy_version_ids", []),
                    "applied_scopes": gm_policy.get("applied_scopes", []),
                },
            },
            "active_gm_policy": gm_policy,
        }
        uri = self.media.write_text_artifact(run_id, "planning_brief.json", planning.model_dump_json(indent=2))
        return StageOutput(
            payload=output,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=[{"type": "planning_brief", "uri": uri, "payload": output}],
        )

    def run_divergence(
        self,
        run_id: str,
        planning: PlanningBrief,
        *,
        variant_count: int,
        gm_policy: dict | None = None,
        creative_specs: dict | None = None,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
    ) -> StageOutput:
        gm_policy = gm_policy or {}
        policy_excerpt = gm_policy.get("stage_guidance") or {}
        director_handoff = self._director_handoff(planning)
        director_plan = director_handoff.get("creative_director_plan") or {}
        production_plan = director_handoff.get("production_plan") or {}
        quality_gates = director_handoff.get("quality_gates") or []
        creative_risk_level = self._creative_risk_level(creative_specs, planning)
        prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="Variant Strategy Agent",
            task_instruction=(
                f"Generate diverse variants from planning: {planning.model_dump()}. "
                f"creative_director_plan={director_plan}. production_plan={production_plan}. quality_gates={quality_gates}. "
                f"creative_risk_level={creative_risk_level}. "
                f"gm_policy={policy_excerpt}. "
                "Each variant must test a distinct commercial hypothesis with non-overlapping hook logic."
            ),
        )
        summary, model_used, estimated_cost = self._chat_complete(provider, model, prompt, runtime_config)
        variants = []
        preferred_angles = (gm_policy.get("angle_priorities") or [])[: max(1, variant_count)]
        emotional_beats = director_plan.get("emotional_beats") or []
        must_preserve = director_plan.get("must_preserve_visuals") or []
        recipe_context = " ".join([
            str(director_plan.get("media_truth_summary") or ""),
            json.dumps(director_plan.get("product_truth_contract") or {}, ensure_ascii=False),
            " ".join(str(item) for item in must_preserve),
        ])
        for i in range(variant_count):
            angle_pool = preferred_angles or planning.strategic_angles or ["product truth"]
            angle = angle_pool[i % max(1, len(angle_pool))]
            variant_id = f"V{i + 1}"
            scene = self._scene_arc_item(director_handoff, i)
            scene_direction = scene.get("scene_direction") or scene.get("beat") or "real product-use scene"
            emotion = emotional_beats[i % len(emotional_beats)] if emotional_beats else "clear buyer confidence"
            hook = self._variant_hook(angle)
            message = self._variant_message(angle, emotion)
            seed_variant = VariantCandidate(variant_id=variant_id, angle=angle, hook=hook, message=message)
            visual_proof_spec = self._variant_visual_proof_spec(
                seed_variant,
                scene_direction,
                recipe_context,
            )
            creative_leap_spec = self._creative_leap_spec(
                seed_variant,
                visual_proof_spec,
                creative_risk_level,
                is_wildcard_slot=i == variant_count - 1,
            )
            variants.append(
                VariantCandidate(
                    variant_id=variant_id,
                    angle=angle,
                    hook=hook,
                    message=message,
                    rationale=(
                        f"Tests `{angle}` against director beat `{scene_direction}`; preserve "
                        f"{', '.join(must_preserve[:3]) or 'core product truth'}."
                    ),
                    visual_proof_spec=visual_proof_spec,
                    creative_leap_spec=creative_leap_spec,
                )
            )
        variant_set = VariantSet(variants=variants)
        experiment_matrix = [
            {
                "variant_id": item.variant_id,
                "test_axis": item.angle,
                "hypothesis": item.message,
                "visual_proof_spec": item.visual_proof_spec,
                "creative_leap_spec": item.creative_leap_spec,
                "director_beat": self._scene_arc_item(director_handoff, idx),
                "success_signal": "Higher qualified click-through or stronger reviewer preference than adjacent variants.",
                "kill_condition": "Weak product relevance, unsupported claim, or visual concept cannot show the product truthfully.",
            }
            for idx, item in enumerate(variants)
        ]
        output = {
            **variant_set.model_dump(),
            "llm_summary": summary,
            "experiment_matrix": experiment_matrix,
            "active_gm_policy": gm_policy,
            "director_strategy": director_handoff,
            "strategy_handoff": self._business_strategy_handoff(
                stage="divergence",
                decisions=[f"created {len(variants)} variant hypotheses", "kept variants bound to director scene beats and distinct test axes"],
                risks=["Variants are heuristic until reviewed against generated media."],
                review_questions=["Are the variants commercially distinct?", "Should any variant be killed before paid generation?"],
            ),
        }
        uri = self.media.write_text_artifact(run_id, "variant_set.json", variant_set.model_dump_json(indent=2))
        return StageOutput(
            payload=output,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=[{"type": "variant_set", "uri": uri, "payload": output}],
        )

    def _run_marketplace_main_image_generation(
        self,
        run_id: str,
        variant_set: VariantSet,
        *,
        intake: ProductIntake | None,
        business_context: dict | None,
        creative_specs: dict | None,
        market: str,
        locale: str,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
    ) -> StageOutput:
        business_context = business_context or {}
        creative_specs = creative_specs or {}
        platform_targets = normalize_platform_targets(creative_specs)
        export_size_px = int(creative_specs.get("export_size_px") or 2000)
        image_size = str(creative_specs.get("image_size") or "1:1")
        visual_summary = intake.asset_media_summary.strip() if intake and intake.asset_media_summary else "No reference media analysis."
        visual_identity = (
            intake.visual_identity.model_dump()
            if intake and hasattr(intake.visual_identity, "model_dump")
            else dict((intake.visual_identity if intake else {}) or {})
        )
        reference_inputs, reference_manifest = self._marketplace_reference_inputs(intake, max_inputs=4)
        roles = [
            {
                "image_role": "compliance_master",
                "style": (
                    "strict marketplace master photo, exact product only, pure #FFFFFF background, no props, no model, "
                    "no text overlay, no watermark, product centered and filling 65-90% of the square frame"
                ),
            },
            {
                "image_role": "premium_white",
                "style": (
                    "premium white-background catalog photo with improved material texture, clean edge refinement, "
                    "natural product-contained lighting, color correction, and no scene background"
                ),
            },
        ]
        estimated_cost = 0.0
        copies: list[CopyVariant] = []
        images: list[ImageAssetRef] = []
        artifacts: list[dict] = []
        image_models_used: set[str] = set()
        audience = business_context.get("target_audience", "marketplace shoppers")
        cta = business_context.get("primary_cta", "View Product")
        product_name = intake.product_name if intake else str(business_context.get("product_name") or "the product")
        product_truth = self._product_truth_contract(intake)

        for idx, item in enumerate(variant_set.variants):
            role = roles[idx % len(roles)]
            image_role = role["image_role"]
            copies.append(
                CopyVariant(
                    variant_id=item.variant_id,
                    primary_text=(
                        f"{item.variant_id}: Marketplace main image candidate for {product_name}; "
                        f"role={image_role}; audience={audience}."
                    ),
                    headline=f"{product_name} Main Image",
                    description=f"White-background product-photo candidate{' for ' + ', '.join(platform_targets) if platform_targets else ''}.",
                    call_to_action=cta,
                )
            )
            prompt = (
                "Edit the uploaded source product media into a source-accurate marketplace main image. "
                "Preserve exact product shape, color, logo/text, material, proportions, and included parts from the references. "
                "Do not invent accessories, packaging, labels, claims, props, people, animals, hands, lifestyle scenes, or text overlays. "
                f"Product visual identity: {json.dumps(visual_identity, ensure_ascii=False)[:2200]}. "
                f"Media summary: {visual_summary[:1600]}. "
                f"Role: {image_role}; style requirements: {role['style']}. "
                f"Output: square {export_size_px}x{export_size_px}px master, pure white background" + (f", marketplace-ready for {', '.join(platform_targets)}" if platform_targets else "") + ". "
                "If source media is low quality, improve lighting, material clarity, edge quality, and color balance while preserving product truth."
                f"{self._qa_repair_prompt_block(runtime_config)}"
            )
            image_uri = ""
            image_source = "placeholder"
            image_model = ""
            image_provider = ""
            error_text = None
            provider_errors: list[dict] = []
            asset_suffix = str((runtime_config or {}).get("asset_name_suffix") or "")
            force_regenerate = bool((runtime_config or {}).get("force_regenerate"))
            image_filename = f"marketplace_{item.variant_id}_{image_role}{asset_suffix}.png"
            existing_image_path = self.media.settings.assets_dir / run_id / image_filename
            try:
                if not force_regenerate and existing_image_path.exists() and existing_image_path.stat().st_size > 1024:
                    image_uri = str(existing_image_path)
                    image_source = "reused_existing"
                else:
                    self._trace_external_task(
                        runtime_config,
                        event_type="external_task_submit_started",
                        message="Submitting image generation task to provider.",
                        payload={
                            "asset_type": "image",
                            "variant_id": item.variant_id,
                            "selected_provider_name": (((runtime_config or {}).get("image") or {}).get("provider_name")) or provider,
                            "selected_model_name": (((runtime_config or {}).get("image") or {}).get("model_name")) or model,
                            "operation": "edit" if reference_inputs else "generate",
                        },
                    )
                    image_result, image_provider, image_model = self._generate_image(
                        fallback_provider=provider,
                        fallback_model=model,
                        prompt=prompt,
                        size=image_size,
                        runtime_config=runtime_config,
                        reference_image_urls=reference_inputs,
                        mode="edit" if reference_inputs else "generate",
                        input_fidelity="high" if reference_inputs else None,
                        official_fallback=creative_specs.get("official_fallback"),
                    )
                    estimated_cost += image_result.estimated_cost
                    image_models_used.add(image_result.model_used or image_model)
                    selected = image_result.images[0] if image_result.images else None
                    if selected:
                        image_bytes, image_source = self._materialize_generated_image(selected)
                    else:
                        image_bytes, image_source = decode_placeholder_png(), "placeholder"
                    image_uri = self.media.write_binary_artifact(run_id, image_filename, image_bytes)
            except Exception as exc:
                error_text = str(exc)
                provider_errors = getattr(exc, "errors", []) or []
                image_uri = self._generation_error_artifact(run_id, item.variant_id, error_text)
                image_source = "generation_error"

            image_ref = ImageAssetRef(
                variant_id=item.variant_id,
                uri=image_uri,
                aspect_ratio=image_size,
                prompt=prompt,
            )
            image_payload = {
                **image_ref.model_dump(),
                "asset_goal": "marketplace_main_image",
                "image_role": image_role,
                "platform_targets": platform_targets,
                "export_size_px": export_size_px,
                "source": image_source,
                "image_provider": image_provider,
                "image_model": image_model,
                "error": error_text,
                "provider_errors": provider_errors,
                "reference_source_count": len(reference_inputs),
                "reference_manifest": reference_manifest,
                "visual_identity": visual_identity,
                "product_truth_contract": product_truth,
            }
            image_payload["visual_qa"] = self._local_media_qa(
                asset_type="image",
                uri=image_uri,
                payload=image_payload,
                expected_ratio=image_size,
            )
            self._attach_image_asset_contract(image_payload)
            self._require_generated_image_asset(image_payload)
            image_payload["marketplace_qa"] = inspect_marketplace_image(
                uri=image_uri,
                payload=image_payload,
                creative_specs=creative_specs,
                visual_identity=visual_identity,
            )
            self._attach_image_asset_contract(image_payload)
            self._require_generated_image_asset(image_payload)
            image_payload["platform_readiness"] = image_payload["marketplace_qa"].get("platform_readiness", {})
            image_payload["export_ready"] = bool(image_payload["marketplace_qa"].get("export_ready"))
            images.append(image_ref)
            artifacts.append({"type": "generated_image", "uri": image_uri, "payload": image_payload})

        bundle = CopyImageBundle(copy_variants=copies, image_assets=images)
        bundle_payload = {
            "asset_goal": "marketplace_main_image",
            "copy_variants": [item.model_dump() for item in copies],
            "image_assets": [artifact["payload"] for artifact in artifacts if artifact["type"] == "generated_image"],
            "visual_identity": visual_identity,
            "reference_manifest": reference_manifest,
            "strategy_handoff": self._business_strategy_handoff(
                stage="copy_image_generation",
                decisions=[
                    f"generated marketplace main-image candidates for {len(copies)} variants",
                    "used source media references when available",
                    f"target_platforms={','.join(platform_targets)}",
                ],
                risks=["Provider reference-edit support may vary; marketplace QA and source-product review are required before export."],
                review_questions=[
                    "Does the generated product match the phone reference exactly?",
                    "Is the background pure white without prop/model leakage?",
                    "Which candidate is ready for marketplace export?",
                ],
            ),
        }
        bundle_uri = self.media.write_text_artifact(run_id, "marketplace_main_image_bundle.json", bundle.model_dump_json(indent=2))
        artifacts.insert(0, {"type": "copy_image_bundle", "uri": bundle_uri, "payload": bundle_payload})
        model_used = f"text={model};image={','.join(sorted(m for m in image_models_used if m)) or 'placeholder'}"
        return StageOutput(
            payload=bundle_payload,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=artifacts,
        )

    def run_copy_image_generation(
        self,
        run_id: str,
        variant_set: VariantSet,
        *,
        intake: ProductIntake | None,
        business_context: dict | None,
        creative_specs: dict | None,
        market: str,
        locale: str,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
        historical_references: list[dict] | None = None,
    ) -> StageOutput:
        if is_marketplace_main_image(creative_specs):
            return self._run_marketplace_main_image_generation(
                run_id,
                variant_set,
                intake=intake,
                business_context=business_context,
                creative_specs=creative_specs,
                market=market,
                locale=locale,
                provider=provider,
                model=model,
                runtime_config=runtime_config,
            )

        business_context = business_context or {}
        creative_specs = creative_specs or {}
        image_size = str(creative_specs.get("image_size") or "1:1")
        resolution = str(creative_specs.get("resolution") or "720p")
        surface_strategy = self._dtc_site_surface_strategy(creative_specs)
        is_dtc_site = bool(surface_strategy)
        image_direction = str(creative_specs.get("image_direction") or "").strip()
        visual_summary = (
            intake.asset_media_summary.strip()
            if intake and intake.asset_media_summary
            else "No reference media analysis."
        )
        product_truth = self._product_truth_contract(intake)
        estimated_cost = 0.0
        text_model_used = model
        reference_inputs = self._reference_image_inputs(intake, extra_references=historical_references)
        spec_reference_inputs = [str(item).strip() for item in (creative_specs.get("reference_image_urls") or []) if str(item).strip()]
        if spec_reference_inputs:
            reference_inputs = [*reference_inputs, *spec_reference_inputs]
        if reference_inputs and visual_summary == "No reference media analysis.":
            vision_prompt = (
                "Analyze uploaded product sample image(s). Return concise product facts for ad generation: "
                "material, color, structure, wearing position, functional highlights, and what should remain consistent."
            )
            try:
                visual_summary, text_model_used, vision_cost = self._chat_complete(
                    provider,
                    model,
                    vision_prompt,
                    runtime_config,
                    image_urls=reference_inputs,
                )
                if intake and intake.asset_media_summary:
                    visual_summary = f"{intake.asset_media_summary}\n\nImage-focus details:\n{visual_summary}"
                estimated_cost += vision_cost
            except Exception as exc:
                visual_summary = f"reference_analysis_failed: {exc}"

        copy_prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="Copy Image Agent",
            task_instruction=(
                (
                    f"Generate concise DTC website creative copy cues for {market} {locale}. "
                    if is_dtc_site
                    else f"Generate concise Meta ad copy variants for US {locale}. "
                )
                + f"dtc_surface_strategy={surface_strategy}. "
                + f"business_context={business_context}, product_visual_summary={visual_summary}, product_truth_contract={product_truth}, variants={variant_set.model_dump()}. "
                + "Keep copy specific, conversion-oriented, and claim-safe. Do not invent certifications or guarantees."
            ),
        )
        try:
            copy_hint, text_model_used, copy_cost = self._chat_complete(provider, model, copy_prompt, runtime_config)
            estimated_cost += copy_cost
        except Exception:
            copy_hint = "focus on clear product value, premium presentation, and buyer confidence."

        value_props = business_context.get("key_value_props", [])
        value_line = ", ".join(value_props[:3]) if value_props else ""
        price = business_context.get("price", "")
        audience = business_context.get("target_audience", "")
        cta = business_context.get("primary_cta", "Shop Now")
        product_name = intake.product_name if intake and intake.product_name else str(business_context.get("product_name") or "the product")

        copies: list[CopyVariant] = []
        images: list[ImageAssetRef] = []
        artifacts: list[dict] = []
        image_models_used: set[str] = set()

        for idx, item in enumerate(variant_set.variants):
            copies.append(
                CopyVariant(
                    variant_id=item.variant_id,
                    primary_text=(
                        f"{item.variant_id}: Built for {audience}. "
                        f"{product_name} with {value_line}. Price {price}."
                    ),
                    headline=f"{item.variant_id}: {product_name} for Everyday Use",
                    description=f"Angle: {item.angle}. Hint: {copy_hint[:140]}",
                    call_to_action=cta,
                )
            )
            image_prompt = (
                (
                    f"Create a DTC website product image for North American market ({market}, {locale}). "
                    if is_dtc_site
                    else f"Create a social media ad image for North American market ({market}, {locale}). "
                )
                + f"{self._dtc_surface_prompt_block(creative_specs)}"
                + self._variant_visual_proof_prompt_block(item)
                + (f"User art direction, within the supplied product facts and placement: {image_direction}. " if image_direction else "")
                + f"Subject: {product_name}. Frame this shot around the variant's intended moment: {item.hook or item.angle}. "
                + "Choose a viewpoint that reveals the supported product detail or action. Use light and background contrast "
                + "to separate that detail at the final crop. Keep identifying features in frame when the placement needs product recognition. "
                + "Give any specified copy area a quiet background; keep it clear of the working detail. "
                + "Include props or graphic treatments when they explain the action or express the supplied creative direction. "
                + "Keep product details aligned with this summary: "
                + f"{visual_summary}. "
                + f"Product truth contract: {product_truth}. "
                + "Preserve reference markings and material appearance. No text overlay. "
                + f"Use aspect ratio {image_size}, target resolution {resolution}. "
                + "Keep the product physically plausible and inspectable."
                + self._qa_repair_prompt_block(runtime_config)
            )
            image_uri = ""
            image_source = "placeholder"
            image_model = ""
            image_provider = ""
            error_text = None
            provider_errors: list[dict] = []
            asset_suffix = str((runtime_config or {}).get("asset_name_suffix") or "")
            force_regenerate = bool((runtime_config or {}).get("force_regenerate"))
            image_filename = f"copy_image_{idx + 1}{asset_suffix}.png"
            existing_image_path = self.media.settings.assets_dir / run_id / image_filename
            task_id = None
            status = None
            raw_response: dict = {}
            try:
                if not force_regenerate and existing_image_path.exists() and existing_image_path.stat().st_size > 1024:
                    image_uri = str(existing_image_path)
                    image_source = "reused_existing"
                else:
                    image_result, image_provider, image_model = self._generate_image(
                        fallback_provider=provider,
                        fallback_model=model,
                        prompt=image_prompt,
                        size=image_size,
                        runtime_config=runtime_config,
                        reference_image_urls=reference_inputs,
                        mode="edit" if reference_inputs else "generate",
                        input_fidelity="high" if reference_inputs else None,
                        official_fallback=creative_specs.get("official_fallback"),
                    )
                    estimated_cost += image_result.estimated_cost
                    image_models_used.add(image_result.model_used or image_model)
                    selected = image_result.images[0] if image_result.images else None
                    task_id = getattr(image_result, "task_id", None) or (getattr(selected, "task_id", None) if selected else None)
                    status = getattr(image_result, "status", None) or (getattr(selected, "status", None) if selected else None)
                    raw_response = getattr(image_result, "raw_response", None) or (getattr(selected, "raw_response", None) if selected else {}) or {}
                    has_image_payload = bool(getattr(selected, "url", None) or getattr(selected, "b64_json", None))
                    if selected and (has_image_payload or not task_id):
                        image_bytes, image_source = self._materialize_generated_image(selected)
                        image_uri = self.media.write_binary_artifact(run_id, image_filename, image_bytes)
                    elif task_id:
                        image_source = "external_task_pending"
                        image_uri = self.media.reserve_binary_artifact(run_id, image_filename)
                        self._trace_external_task(
                            runtime_config,
                            event_type="external_task_submitted",
                            message=f"Image generation task submitted: {task_id}.",
                            payload={
                                "asset_type": "image",
                                "variant_id": item.variant_id,
                                "external_task_id": task_id,
                                "generation_status": status,
                                "selected_provider_name": image_provider,
                                "selected_model_name": image_model,
                            },
                        )
                    else:
                        image_bytes, image_source = decode_placeholder_png(), "placeholder"
                        image_uri = self.media.write_binary_artifact(run_id, image_filename, image_bytes)
            except Exception as exc:
                error_text = str(exc)
                provider_errors = getattr(exc, "errors", []) or []
                image_uri = self._generation_error_artifact(run_id, item.variant_id, error_text)
                image_source = "generation_error"

            image_ref = ImageAssetRef(
                variant_id=item.variant_id,
                uri=image_uri,
                aspect_ratio=image_size,
                prompt=image_prompt,
            )
            image_payload = {
                **image_ref.model_dump(),
                "source": image_source,
                "image_provider": image_provider,
                "image_model": image_model,
                "error": error_text,
                "provider_errors": provider_errors,
                "external_task_id": task_id,
                "generation_status": status,
                "external_task_state": self._external_task_state(
                    asset_type="image",
                    task_id=task_id,
                    status=status,
                    provider_name=image_provider,
                    model_name=image_model,
                    event="submitted",
                ),
                "raw_response": raw_response,
                "product_truth_contract": product_truth,
                "visual_proof_spec": self._variant_visual_proof_spec(item),
                "creative_leap_spec": dict(item.creative_leap_spec or {}),
            }
            image_payload["reference_source_count"] = len(historical_references or [])
            if image_source == "generation_error":
                image_payload["visual_qa"] = {
                    "status": "fail",
                    "score": 0.0,
                    "flags": ["visual_qa_generation_error"],
                    "checks": [{"key": "generation_error", "status": "fail", "message": error_text or "image generation failed"}],
                }
            else:
                image_payload["visual_qa"] = self._local_media_qa(
                    asset_type="image",
                    uri=image_uri,
                    payload=image_payload,
                    expected_ratio=image_size,
                )
            self._attach_image_asset_contract(image_payload)
            images.append(image_ref)
            artifacts.append(
                {
                    "type": "generated_image",
                    "uri": image_uri,
                    "payload": image_payload,
                }
            )

        bundle = CopyImageBundle(copy_variants=copies, image_assets=images)
        image_asset_payloads = [artifact["payload"] for artifact in artifacts if artifact["type"] == "generated_image"]
        pending_statuses = {"submitted", "queued", "pending", "processing", "running"}
        has_pending_image = any(
            str(payload.get("source") or "") == "external_task_pending"
            or str(payload.get("generation_status") or "").lower() in pending_statuses
            for payload in image_asset_payloads
        )
        has_reviewable_image = any(
            not (payload.get("error") or (payload.get("image_asset_contract") or {}).get("blocking"))
            for payload in image_asset_payloads
        )
        if image_asset_payloads and not has_reviewable_image and not has_pending_image:
            failure_flags = sorted(
                {
                    str(flag)
                    for payload in image_asset_payloads
                    for flag in (payload.get("image_asset_contract") or {}).get("flags") or []
                }
            )
            reason = ", ".join(failure_flags) or "all_images_failed"
            raise RuntimeError(f"image provider produced no reviewable assets: {reason}")

        bundle_payload = {
            "copy_variants": [item.model_dump() for item in copies],
            "image_assets": image_asset_payloads,
            "strategy_handoff": self._business_strategy_handoff(
                stage="copy_image_generation",
                decisions=[
                    f"generated copy/image candidates for {len(copies)} variants",
                    "kept no-text-overlay image prompts",
                    f"site_surface={surface_strategy.get('site_surface') or 'none'}",
                ],
                risks=["Image provider may still introduce malformed product details; visual QA and human review required."],
                review_questions=["Is the product visibly correct?", "Does copy avoid unsupported claims?", "Which image has strongest product-forward composition?"],
            ),
        }
        bundle_uri = self.media.write_text_artifact(run_id, "copy_image_bundle.json", bundle.model_dump_json(indent=2))
        artifacts.insert(0, {"type": "copy_image_bundle", "uri": bundle_uri, "payload": bundle_payload})
        model_used = f"text={text_model_used};image={','.join(sorted(m for m in image_models_used if m)) or 'placeholder'}"
        return StageOutput(
            payload=bundle_payload,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=artifacts,
        )

    def _build_tiktok_payload(
        self,
        *,
        product_name: str,
        primary_value: str,
        cta: str,
        tiktok_style: str,
        video_duration: float,
        message: str | None = None,
    ) -> dict:
        """Build TikTok-specific script payload from creative specs.

        The LLM provides hook/script/shot_list, but the TikTok-specific
        structure (style, timing, on_screen_text, compliance_notes) should
        be built deterministically from creative_specs.
        """
        opening_hook = f"POV: your {product_name} solves this in seconds"
        proof_points = [primary_value]
        if message:
            proof_points.append(message)
        proof_points = proof_points[:2]
        if tiktok_style == "direct_response_ad":
            opening_hook = f"Stop scrolling if you need {primary_value}"
            cta_intensity = "strong"
        elif tiktok_style == "shop_account_content":
            opening_hook = f"Packing one small upgrade from our shop: {product_name}"
            cta_intensity = "soft"
        else:
            cta_intensity = "medium"
        cta_duration = min(4.0, max(2.0, video_duration * 0.15))
        cta_start = max(2.0, video_duration - cta_duration)
        return {
            "style": tiktok_style,
            "opening_hook": opening_hook,
            "on_screen_text": [
                opening_hook,
                f"Proof: {primary_value}",
                cta,
            ],
            "voiceover_lines": [
                opening_hook,
                f"Here is how {product_name} helps with {primary_value}.",
                f"If this fits your routine, {cta}.",
            ],
            "shot_timing": [
                {
                    "start": 0,
                    "end": 2,
                    "visual": "fast vertical product reveal in a realistic use scene",
                    "text_overlay": opening_hook,
                    "intent": "thumb_stop",
                },
                {
                    "start": 2,
                    "end": cta_start,
                    "visual": "close product demo with the key proof point visible",
                    "text_overlay": f"Proof: {primary_value}",
                    "intent": "proof",
                },
                {
                    "start": cta_start,
                    "end": video_duration,
                    "visual": "product-forward end frame with clear next step",
                    "text_overlay": cta,
                    "intent": "cta",
                },
            ],
            "product_proof_points": proof_points,
            "cta": cta,
            "compliance_notes": [
                "Do not invent certifications, discounts, platform trends, or unsupported performance claims.",
                f"CTA intensity: {cta_intensity}.",
            ],
        }

    def run_video_scripting(
        self,
        run_id: str,
        variant_set: VariantSet,
        *,
        intake: ProductIntake | None,
        business_context: dict | None = None,
        provider: str,
        model: str,
        creative_specs: dict | None = None,
        pipeline_mode: str | None = None,
        runtime_config: dict | None = None,
        reference_bundle: dict | None = None,
        planning: PlanningBrief | dict | None = None,
    ) -> StageOutput:
        business_context = business_context or {}
        creative_specs = creative_specs or {}
        reference_bundle = reference_bundle or {}
        director_handoff = self._director_handoff(planning)
        director_plan = director_handoff.get("creative_director_plan") or {}
        production_plan = director_handoff.get("production_plan") or {}
        quality_gates = director_handoff.get("quality_gates") or []
        is_tiktok_shop = pipeline_mode == "tiktok_shop_video"
        tiktok_style = str(creative_specs.get("tiktok_video_style") or "ugc_demo")
        product_context = self._video_product_context(
            intake=intake,
            business_context=business_context,
            creative_specs=creative_specs,
        )
        visual_proof_specs = self._variant_visual_proof_specs(variant_set)
        creative_leap_specs = self._variant_creative_leap_specs(variant_set)
        product_context["visual_proof_specs"] = visual_proof_specs
        if creative_leap_specs:
            product_context["creative_leap_specs"] = creative_leap_specs
        if director_handoff:
            product_context["director_plan"] = director_handoff
        prompt_grammar = dict(director_plan.get("prompt_grammar") or {})
        if prompt_grammar:
            product_context["prompt_grammar"] = prompt_grammar
        prompt_grammar_line = self._prompt_grammar_text(prompt_grammar)
        reference_summary = {
            "image_count": len(reference_bundle.get("images") or []),
            "frame_count": len(reference_bundle.get("frames") or []),
        }
        media_summary = str(product_context.get("media_summary") or "")
        product_name = str(product_context.get("product_name") or "the product")
        product_truth = product_context.get("product_truth_contract") or {}
        value_props = [str(item) for item in (product_context.get("value_props") or []) if str(item).strip()]
        audience = str(product_context.get("audience") or "target buyers")
        cta = str(product_context.get("primary_cta") or "Shop Now")
        generation_spec = self._video_generation_spec(creative_specs)
        prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="Video Script Agent",
            task_instruction=(
                "Generate video hooks and scripts with the product context. "
                f"product={product_name}, audience={audience}, value_props={value_props}, "
                f"media_summary={media_summary}, variants={variant_set.model_dump()}. "
                f"product_truth_contract={product_truth}. "
                f"visual_proof_specs={visual_proof_specs}. "
                f"creative_leap_specs={creative_leap_specs}. "
                f"reference_summary={reference_summary}. "
                f"generation_spec={generation_spec}. "
                f"creative_director_plan={director_plan}. production_plan={production_plan}. quality_gates={quality_gates}. "
                f"prompt_grammar={prompt_grammar}. "
                "Make every shot filmable, product-specific, and constrained by realistic product handling. "
                f"{self._human_motion_risk_instruction(' '.join([product_name, media_summary, audience, str(variant_set.model_dump()), str(director_plan)]))} "
                "Carry the director scene arc, emotional beats, product-truth gates, visual_proof_specs, creative_leap_specs, and prompt_grammar into hooks, shot_plan, and segments. "
                "Every shot_plan and segment must prove its proof_mechanism and avoid semantic_fail_conditions. "
                "For each variant, also output a structured shot_plan array with 3-4 shot objects. "
                "Each shot must have: shot_id, variant_id, intent (one of: thumb_stop, product_proof, usage_demo, cta_packshot), "
                "first_frame with description and visible_product_elements, "
                "optional last_frame, motion_description, audio_description, text_overlay, "
                "and product_continuity_constraints (e.g. color_match, scale_consistent, material_match). "
                "If generation_spec.duration is above 15 seconds, also output segments where every segment is 15 seconds or shorter. "
                "Each segment must include: segment_id, variant_id, duration_seconds, scene, shot_intent, first_frame_prompt, "
                "last_frame_prompt, motion_prompt, transition_to_next, variation_type (small|medium|large), and continuity_constraints. "
                "Return ONLY valid JSON shaped as {\"scripts\":[{\"variant_id\":\"V1\",\"hook\":\"...\",\"script\":\"...\","
                "\"shot_list\":[\"...\"],\"shot_plan\":[{\"shot_id\":\"shot_1\",\"variant_id\":\"V1\",\"intent\":\"thumb_stop\","
                "\"first_frame\":{\"description\":\"...\",\"visible_product_elements\":[\"...\"]},\"motion_description\":\"...\","
                "\"text_overlay\":\"...\",\"product_continuity_constraints\":[\"...\"]}],\"segments\":[{\"segment_id\":\"V1_S1\","
                "\"variant_id\":\"V1\",\"duration_seconds\":12,\"scene\":\"...\",\"shot_intent\":\"thumb_stop\","
                "\"first_frame_prompt\":\"...\",\"last_frame_prompt\":\"...\",\"motion_prompt\":\"...\","
                "\"transition_to_next\":\"match_cut\",\"variation_type\":\"small\",\"continuity_constraints\":[\"...\"]}]}]}."
                f"{self._qa_repair_prompt_block(runtime_config)}"
            ),
        )
        response_text, model_used, estimated_cost = self._chat_complete(provider, model, prompt, runtime_config)
        scripts = []
        try:
            parsed = self._parse_llm_json(response_text, schema_key="scripts")
        except ValueError:
            model_used = model_used + ":fallback_to_template"
            for item in variant_set.variants:
                primary_value = (
                    value_props[(len(scripts)) % len(value_props)]
                    if value_props
                    else str(item.angle or item.message or "core product benefit")
                )
                hook_base = item.hook or item.angle or primary_value
                tiktok_payload = None
                if is_tiktok_shop:
                    video_duration = float(creative_specs.get("video_duration_seconds") or 12)
                    tiktok_payload = self._build_tiktok_payload(
                        product_name=product_name,
                        primary_value=primary_value,
                        cta=cta,
                        tiktok_style=tiktok_style,
                        video_duration=video_duration,
                        message=item.message,
                )
                hook_line = item.hook or f"{item.variant_id}: {product_name} for {primary_value}"
                visual_proof_spec = visual_proof_specs.get(item.variant_id) or self._variant_visual_proof_spec(item)
                creative_leap_spec = dict(item.creative_leap_spec or {})
                leap_plan = [str(value) for value in (creative_leap_spec.get("shot_device_plan") or []) if str(value).strip()]
                emotions = director_plan.get("emotional_beats") or []
                must_preserve = director_plan.get("must_preserve_visuals") or []
                scene_hints = director_plan.get("scene_hints") or []
                shot_list = []
                intents = ["thumb_stop", "product_proof", "usage_demo", "cta_packshot"]
                for i, intent in enumerate(intents):
                    scene = self._scene_arc_item(director_handoff, i)
                    direction = scene.get("scene_direction") or [
                        f"hook reveal of {product_name} in a realistic use context tied to the submitted brief",
                        f"close-up of {product_name} showing {primary_value}",
                        f"practical demo of {product_name} for {audience}",
                        f"product-forward CTA end frame: {cta}",
                    ][i]
                    emotion = emotions[i % len(emotions)] if emotions else "buyer confidence"
                    preserve = ", ".join(must_preserve[:3]) or product_name
                    overlays = [
                        f"Wait, this changes the whole look",
                        f"Proof: {primary_value}",
                        f"From {scene_hints[0] if scene_hints else 'one real moment'} to the next",
                        cta,
                    ]
                    must_show_values = [
                        str(value)
                        for value in visual_proof_spec.get("must_show", [])
                        if str(value).strip() and not str(value).startswith(f"{item.variant_id}:")
                    ]
                    must_show = ", ".join(must_show_values[:3]) or primary_value
                    avoid_candidates = [str(value) for value in visual_proof_spec.get("must_not_show", []) if str(value).strip()]
                    avoid_values = [next((value for value in avoid_candidates if "generic lifestyle" not in value.lower()), avoid_candidates[0])] if avoid_candidates else list(visual_proof_spec.get("semantic_fail_conditions", [])[:1])
                    avoid = ", ".join(str(value) for value in avoid_values)
                    avoid_part = f"; avoid: {avoid}" if avoid else ""
                    leap_part = f"; creative leap: {leap_plan[min(i, len(leap_plan) - 1)]}" if leap_plan else ""
                    shot_list.append(f"{intent}: {direction}; product proof: show {must_show}{avoid_part}{leap_part}; overlay: {overlays[i]}; preserve: {preserve}; emotion: {emotion}")
                fallback_shot_plan: list[ShotPlanItem] = []
                for i, shot_text in enumerate(shot_list):
                    intent = intents[i] if i < len(intents) else "product_demo"
                    motion_description = shot_text
                    if prompt_grammar.get("camera_style"):
                        motion_description = f"{motion_description} Camera behavior: {prompt_grammar['camera_style']}."
                    fallback_shot_plan.append(ShotPlanItem(
                        shot_id=f"shot_{i+1}",
                        variant_id=item.variant_id,
                        intent=intent,
                        first_frame=ShotFramePlan(
                            description=shot_text,
                            visible_product_elements=[product_name],
                        ),
                        motion_description=motion_description,
                        audio_description=str(prompt_grammar.get("audio_plan") or ""),
                        product_continuity_constraints=[
                            *[str(item) for item in visual_proof_spec.get("must_show", [])],
                            *[f"avoid: {item}" for item in visual_proof_spec.get("semantic_fail_conditions", [])],
                        ],
                    ))
                segments = self._build_video_segments(
                    variant_id=item.variant_id,
                    shot_plan=fallback_shot_plan,
                    shot_list=shot_list,
                    total_seconds=int(generation_spec.get("duration") or 8),
                    creative_specs=creative_specs,
                    product_name=product_name,
                    product_truth=product_truth,
                )
                scripts.append(
                    VideoScriptItem(
                        variant_id=item.variant_id,
                        hook=hook_line,
                        script=(
                            f"Cold open: a real buyer hesitates before a plan, then {product_name} changes the mood in-frame. "
                            f"Beat 2: close proof of {primary_value}, with the preserved product details visible. "
                            f"Beat 3: show the look moving through {scene_hints[0] if scene_hints else 'a specific daily scene'} for {audience}. "
                            f"Final beat: hold a clean product-forward frame and say {cta}. "
                            f"Variant hook: {hook_base}. Variant message: {item.message}"
                        ),
                        shot_list=shot_list,
                        shot_plan=fallback_shot_plan,
                        segments=segments,
                        tiktok=tiktok_payload,
                    )
                )
        else:
            for entry in parsed["scripts"]:
                entry_variant_id = entry.get("variant_id", f"V{len(scripts)+1}")
                visual_proof_spec = visual_proof_specs.get(entry_variant_id) or {}
                tiktok_payload = None
                if is_tiktok_shop:
                    entry_vid = entry_variant_id
                    matching_variant = None
                    for v in variant_set.variants:
                        if v.variant_id == entry_vid:
                            matching_variant = v
                            break
                    if matching_variant:
                        primary_value = str(
                            matching_variant.angle or matching_variant.message or "core product benefit"
                        )
                        message = matching_variant.message
                    elif value_props:
                        primary_value = value_props[(len(scripts)) % len(value_props)]
                        message = None
                    else:
                        primary_value = "core product benefit"
                        message = None
                    video_duration = float(creative_specs.get("video_duration_seconds") or 12)
                    tiktok_payload = self._build_tiktok_payload(
                        product_name=product_name,
                        primary_value=primary_value,
                        cta=cta,
                        tiktok_style=tiktok_style,
                        video_duration=video_duration,
                        message=message,
                    )
                # Parse shot_plan from LLM response if present
                shot_plan_raw = entry.get("shot_plan") or []
                shot_plan: list[ShotPlanItem] = []
                for sp in shot_plan_raw:
                    try:
                        ff = sp.get("first_frame", {})
                        lf = sp.get("last_frame")
                        shot_plan.append(ShotPlanItem(
                            shot_id=sp.get("shot_id", f"shot_{len(shot_plan)+1}"),
                            variant_id=sp.get("variant_id", entry_variant_id),
                            intent=sp.get("intent", "product_demo"),
                            duration_seconds=sp.get("duration_seconds"),
                            first_frame=ShotFramePlan(
                                description=ff.get("description", ""),
                                visible_product_elements=ff.get("visible_product_elements", []),
                            ),
                            last_frame=ShotFramePlan(
                                description=lf.get("description", ""),
                                visible_product_elements=lf.get("visible_product_elements", []),
                            ) if lf else None,
                            motion_description=sp.get("motion_description", ""),
                            audio_description=sp.get("audio_description", ""),
                            text_overlay=sp.get("text_overlay", ""),
                            product_continuity_constraints=[
                                *[str(item) for item in (sp.get("product_continuity_constraints") or [])],
                                *[str(item) for item in visual_proof_spec.get("must_show", [])],
                                *[f"avoid: {item}" for item in visual_proof_spec.get("semantic_fail_conditions", [])],
                            ],
                        ))
                    except Exception:
                        continue
                segments: list[VideoSegmentPlan] = []
                for segment in entry.get("segments") or []:
                    try:
                        segments.append(VideoSegmentPlan.model_validate(segment))
                    except Exception:
                        continue
                if not segments:
                    segments = self._build_video_segments(
                        variant_id=entry_variant_id,
                        shot_plan=shot_plan,
                        shot_list=entry.get("shot_list", []),
                        total_seconds=int(generation_spec.get("duration") or 8),
                        creative_specs=creative_specs,
                        product_name=product_name,
                        product_truth=product_truth,
                    )
                else:
                    segments = self._sequence_long_video_segments(segments, product_name)
                    segments = self._apply_segment_contracts(
                        segments,
                        product_truth=product_truth,
                        product_name=product_name,
                    )
                scripts.append(
                    VideoScriptItem(
                        variant_id=entry_variant_id,
                        hook=entry.get("hook", ""),
                        script=entry.get("script", ""),
                        shot_list=entry.get("shot_list", []), shot_plan=shot_plan,
                        segments=segments,
                        tiktok=tiktok_payload,
                    )
                )
        pack = VideoScriptPack(
            scripts=scripts,
            product_context=product_context,
            generation_spec=generation_spec,
        )
        payload = {
            **pack.model_dump(),
            "reference_summary": reference_summary,
            "director_strategy": director_handoff,
            "strategy_handoff": self._business_strategy_handoff(
                stage="video_scripting",
                decisions=[f"generated scripts for {len(scripts)} variants", "required director scene arc, product handling, and physical continuity"],
                risks=["Script quality still depends on storyboard and video provider following continuity constraints."],
                review_questions=["Does each hook feel native to TikTok?", "Can every shot be generated without breaking product logic?"],
            ),
        }
        uri = self.media.write_text_artifact(run_id, "video_scripts.json", pack.model_dump_json(indent=2))
        return StageOutput(
            payload=payload,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=[{"type": "video_script_pack", "uri": uri, "payload": payload}],
        )

    def run_storyboard_image_generation(
        self,
        run_id: str,
        script_pack: VideoScriptPack,
        *,
        provider: str,
        model: str,
        creative_specs: dict | None = None,
        runtime_config: dict | None = None,
        historical_references: list[dict] | None = None,
        intake: ProductIntake | None = None,
        planning: PlanningBrief | dict | None = None,
    ) -> StageOutput:
        creative_specs = creative_specs or {}
        director_handoff = self._director_handoff(planning) or dict((script_pack.product_context or {}).get("director_plan") or {})
        director_plan = director_handoff.get("creative_director_plan") or {}
        production_plan = director_handoff.get("production_plan") or {}
        quality_gates = director_handoff.get("quality_gates") or []
        generation_spec = {**(script_pack.generation_spec or {}), **self._video_generation_spec(creative_specs)}
        reference_summary = {
            "image_count": len(historical_references or []),
        }
        # Build reference URLs from historical best images
        historical_frame_refs: list[str] = []
        for ref in (historical_references or [])[:2]:
            data_url = ref.get("uri")
            if isinstance(data_url, str) and data_url:
                historical_frame_refs.append(data_url)
        image_size = self._normalize_media_size(str(generation_spec.get("size") or creative_specs.get("video_size") or creative_specs.get("image_size") or "9:16"))
        uploaded_refs = self._reference_image_inputs(intake)
        runtime_extra = dict((runtime_config or {}).get("extra") or {})
        candidate_count = normalize_storyboard_candidate_count(
            creative_specs.get("storyboard_candidate_count", runtime_extra.get("storyboard_candidate_count"))
        )
        product_context = script_pack.product_context or {}
        product_name = str(product_context.get("product_name") or "the product")
        product_truth = product_context.get("product_truth_contract") or {}
        visual_proof_specs = product_context.get("visual_proof_specs") or {}
        creative_leap_specs = product_context.get("creative_leap_specs") or {}
        prompt_grammar = dict(product_context.get("prompt_grammar") or director_plan.get("prompt_grammar") or {})
        prompt_grammar_line = self._prompt_grammar_text(prompt_grammar)
        prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="Storyboard Agent",
            task_instruction=(
                f"Create storyboard frames from scripts: {script_pack.model_dump()}. "
                f"reference_summary={reference_summary}. "
                f"product_truth_contract={product_truth}. "
                f"visual_proof_specs={visual_proof_specs}. "
                f"creative_leap_specs={creative_leap_specs}. "
                f"prompt_grammar={prompt_grammar}. "
                f"creative_director_plan={director_plan}. production_plan={production_plan}. quality_gates={quality_gates}. "
                "Treat storyboard as the visual QA plan before video generation: continuity, object logic, product visibility, prompt grammar, and visual proof mechanism."
            ),
        )
        estimated_cost = 0.0
        error_text = None
        response_text: str | None = None
        try:
            response_text, model_used, estimated_cost = self._chat_complete(provider, model, prompt, runtime_config)
        except Exception as exc:
            model_used = f"{provider}/{model}:storyboard_text_unavailable"
            error_text = str(exc)
        llm_frame_prompts: dict[str, dict] = {}
        if response_text is not None:
            try:
                parsed = self._parse_llm_json(response_text, schema_key="frames")
                for frame_data in parsed["frames"]:
                    llm_frame_prompts[frame_data["frame_id"]] = frame_data
            except ValueError:
                model_used = model_used + ":fallback_to_template"
        frames: list[dict] = []
        artifacts: list[dict] = []
        for script in script_pack.scripts:
            visual_proof_spec = dict(visual_proof_specs.get(script.variant_id) or {})
            creative_leap_spec = dict(creative_leap_specs.get(script.variant_id) or {})
            proof_block = (
                f"Visual proof spec: {self._visual_proof_prompt_text(visual_proof_spec)} "
                "Frame must support the proof_mechanism and avoid semantic_fail_conditions. "
                if visual_proof_spec
                else ""
            )
            leap_block = f"{self._creative_leap_prompt_text(creative_leap_spec)} " if creative_leap_spec else ""
            for idx in range(3):
                shot = script.shot_list[idx] if idx < len(script.shot_list) else script.hook
                frame_id = f"{script.variant_id}_F{idx + 1}"
                llm_frame = llm_frame_prompts.get(frame_id)
                scene = self._scene_arc_item(director_handoff, idx)
                emotions = director_plan.get("emotional_beats") or []
                emotion = emotions[idx % len(emotions)] if emotions else "buyer confidence"
                must_preserve = ", ".join((director_plan.get("must_preserve_visuals") or product_truth.get("must_preserve") or [])[:4]) or product_name
                gate_names = (
                    ", ".join(str((g.get("gate") if isinstance(g, dict) else g) or g) for g in quality_gates[:3])
                    if quality_gates
                    else "product truth, continuity, channel fit"
                )
                shot_plan_item = script.shot_plan[idx] if idx < len(script.shot_plan) else None
                shot_contract = ""
                if shot_plan_item:
                    shot_contract = (
                        f" First frame: {shot_plan_item.first_frame.description}. "
                        f"Motion: {shot_plan_item.motion_description}. "
                        f"Continuity: {', '.join(shot_plan_item.product_continuity_constraints)}."
                    )
                if llm_frame is not None:
                    frame_prompt = str(llm_frame["prompt"])
                    if director_handoff:
                        frame_prompt = (
                            f"{frame_prompt} Director beat: {scene}. Emotional target: {emotion}. Preserve: {must_preserve}. "
                            f"{proof_block}{leap_block}Quality gates: {gate_names}. No category assumptions."
                        )
                else:
                    tiktok_details = script.tiktok.model_dump() if script.tiktok else {}
                    style_line = (
                        f"TikTok style: {tiktok_details.get('style')}. "
                        f"Opening hook: {tiktok_details.get('opening_hook')}. "
                        if tiktok_details
                        else ""
                    )
                    frame_prompt = (
                        f"Create a realistic storyboard frame for {product_name}. "
                        f"Variant {script.variant_id}. Shot: {shot}. Hook: {script.hook}. "
                        f"Director beat: {scene}. Emotional target: {emotion}. "
                        f"{proof_block}{leap_block}"
                        f"Preserve product facts: {must_preserve}. Quality gates: {gate_names}.{shot_contract} "
                        f"Historical reference summary: {reference_summary}. "
                        f"{style_line}"
                        f"{prompt_grammar_line} "
                        "Use a clean previsualization style suitable for human review before video generation. "
                        "No text overlay. Product-forward composition. "
                        "Work across product categories: show real use context, hands, environment, scale, or buyer outcome as appropriate; "
                        "use a person only when the product or brief calls for human use, and do not force fashion/model framing for non-wearables. "
                        f"{self._video_prompt_quality_block(product_context)}"
                        f"{self._qa_repair_prompt_block(runtime_config)}"
                    )
                frame_prompt += (
                    " Frame the shot's intended action so its contact point or outcome reads clearly. "
                    "Carry product scale, light direction and distinguishing details across adjacent shots "
                    "where the scene is continuous. Preserve planned changes in viewpoint."
                )
                source = "placeholder"
                image_provider = ""
                image_model = ""
                frame_error = error_text
                provider_errors: list[dict] = []
                asset_suffix = str((runtime_config or {}).get("asset_name_suffix") or "")
                candidate_frames: list[dict] = []
                for candidate_idx in range(candidate_count):
                    candidate_prompt = frame_prompt if candidate_count == 1 else f"{frame_prompt}\nCandidate index: {candidate_idx + 1}."
                    candidate_source = "placeholder"
                    candidate_provider = ""
                    candidate_model = ""
                    candidate_error = error_text
                    candidate_provider_errors: list[dict] = []
                    candidate_uri = ""
                    task_id = None
                    status = None
                    raw_response: dict = {}
                    try:
                        image_result, candidate_provider, candidate_model = self._generate_image(
                            fallback_provider=provider,
                            fallback_model=model,
                            prompt=candidate_prompt,
                            size=image_size,
                            runtime_config=runtime_config,
                            reference_image_urls=(uploaded_refs + historical_frame_refs)[:4] or None,
                        )
                        estimated_cost += image_result.estimated_cost
                        selected = image_result.images[0] if image_result.images else None
                        task_id = getattr(image_result, "task_id", None) or (getattr(selected, "task_id", None) if selected else None)
                        status = getattr(image_result, "status", None) or (getattr(selected, "status", None) if selected else None)
                        raw_response = getattr(image_result, "raw_response", None) or (getattr(selected, "raw_response", None) if selected else {}) or {}
                        if selected and (selected.url or selected.b64_json):
                            frame_bytes, candidate_source = self._materialize_generated_image(selected)
                            candidate_uri = self.media.write_binary_artifact(
                                run_id,
                                f"{script.variant_id}_storyboard_{idx + 1}_cand_{candidate_idx + 1}{asset_suffix}.png",
                                frame_bytes,
                            )
                            if candidate_source != "placeholder":
                                candidate_error = None
                        elif task_id:
                            candidate_source = "external_task_pending"
                            candidate_uri = self.media.reserve_binary_artifact(
                                run_id,
                                f"{script.variant_id}_storyboard_{idx + 1}_cand_{candidate_idx + 1}{asset_suffix}.png",
                            )
                            candidate_error = None
                        else:
                            frame_bytes, candidate_source = decode_placeholder_png(), "placeholder"
                            candidate_uri = self.media.write_binary_artifact(
                                run_id,
                                f"{script.variant_id}_storyboard_{idx + 1}_cand_{candidate_idx + 1}{asset_suffix}.png",
                                frame_bytes,
                            )
                    except Exception as exc:
                        candidate_error = str(exc)
                        candidate_provider_errors = getattr(exc, "errors", []) or []
                        candidate_uri = self._generation_error_artifact(
                            run_id,
                            f"{script.variant_id}_storyboard_{idx + 1}_cand_{candidate_idx + 1}",
                            candidate_error,
                        )
                    candidate_payload = {
                        "variant_id": script.variant_id,
                        "frame_id": frame_id,
                        "candidate_index": candidate_idx,
                        "prompt": candidate_prompt,
                        "image_uri": candidate_uri,
                        "source": candidate_source,
                        "image_provider": candidate_provider,
                        "image_model": candidate_model,
                        "error": candidate_error,
                        "provider_errors": candidate_provider_errors,
                        "external_task_id": task_id,
                        "generation_status": status,
                        "raw_response": raw_response,
                        "product_truth_contract": product_truth,
                        "visual_proof_spec": visual_proof_spec,
                        "creative_leap_spec": creative_leap_spec,
                    }
                    candidate_payload["visual_qa"] = self._local_media_qa(
                        asset_type="storyboard_frame",
                        uri=candidate_uri,
                        payload=candidate_payload,
                        expected_ratio=image_size,
                    )
                    candidate_frames.append(candidate_payload)

                best_candidate = max(
                    candidate_frames,
                    key=lambda item: float(((item.get("visual_qa") or {}).get("score")) or 0.0),
                )
                source = str(best_candidate.get("source") or source)
                image_provider = str(best_candidate.get("image_provider") or "")
                image_model = str(best_candidate.get("image_model") or "")
                frame_error = best_candidate.get("error")
                provider_errors = list(best_candidate.get("provider_errors") or [])
                frame_uri = str(best_candidate.get("image_uri") or "")
                frame = {
                    "variant_id": script.variant_id,
                    "frame_id": frame_id,
                    "prompt": frame_prompt,
                    "image_uri": frame_uri,
                    "source": source,
                    "image_provider": image_provider,
                    "image_model": image_model,
                    "error": frame_error,
                    "provider_errors": provider_errors,
                    "external_task_id": best_candidate.get("external_task_id"),
                    "generation_status": best_candidate.get("generation_status"),
                    "raw_response": best_candidate.get("raw_response") or {},
                    "selected_candidate_index": int(best_candidate.get("candidate_index") or 0),
                    "candidate_frames": candidate_frames,
                    "visual_proof_spec": visual_proof_spec,
                    "creative_leap_spec": creative_leap_spec,
                }
                frame["reference_source_count"] = len(historical_references or [])
                frame["visual_qa"] = best_candidate.get("visual_qa") or self._local_media_qa(
                    asset_type="storyboard_frame",
                    uri=frame_uri,
                    payload=frame,
                    expected_ratio=image_size,
                )
                frames.append(frame)
                artifacts.append({"type": "storyboard_frame", "uri": frame_uri, "payload": frame})
        output = {
            "frames": frames,
            "director_strategy": director_handoff,
            "strategy_handoff": self._business_strategy_handoff(
                stage="storyboard_image_generation",
                decisions=[f"created {len(frames)} storyboard frames", "each frame prompt repeats director, product continuity, and category-fit constraints"],
                risks=["Storyboard image QA is basic; complex physics still requires reviewer/model inspection."],
                review_questions=["Do storyboard frames preserve product continuity?", "Should any variant be regenerated before video generation?"],
            ),
        }
        uri = self.media.write_text_artifact(run_id, "storyboard_pack.json", str(output))
        artifacts.append({"type": "storyboard_pack", "uri": uri, "payload": output})
        return StageOutput(
            payload=output,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=artifacts,
        )

    def _generate_video_clip_payload(
        self,
        *,
        run_id: str,
        variant_id: str,
        video_prompt: str,
        video_size: str,
        resolution: str,
        duration_seconds: int,
        generation_spec: dict,
        provider: str,
        model: str,
        runtime_config: dict | None,
        video_filename: str,
        force_regenerate: bool,
    ) -> tuple[dict, float, str]:
        source = "placeholder"
        error_text = None
        model_used = ""
        provider_used = ""
        generation_status = None
        external_task_id = None
        result_url = None
        last_frame_url = None
        raw_response: dict = {}
        provider_errors: list[dict] = []
        estimated_cost = 0.0
        video_uri = ""
        existing_video_path = self.media.settings.assets_dir / run_id / video_filename
        try:
            if not force_regenerate and self._artifact_has_payload(str(existing_video_path)):
                video_uri = str(existing_video_path)
                source = "reused_existing"
                generation_status = "completed"
            else:
                self._trace_external_task(
                    runtime_config,
                    event_type="external_task_submit_started",
                    message="Submitting video generation task to provider.",
                    payload={
                        "asset_type": "video",
                        "variant_id": variant_id,
                        "selected_provider_name": ((runtime_config or {}).get("video") or {}).get("provider_name") or provider,
                        "selected_model_name": ((runtime_config or {}).get("video") or {}).get("model_name") or model,
                        "duration_seconds": duration_seconds,
                        "size": video_size,
                        "resolution": resolution,
                    },
                )
                video_result, provider_used, model_used = self._generate_video_submit_only(
                    fallback_provider=provider,
                    fallback_model=model,
                    prompt=video_prompt,
                    size=video_size,
                    resolution=resolution,
                    duration_seconds=duration_seconds,
                    video_payload=generation_spec,
                    runtime_config=runtime_config,
                )
                estimated_cost += video_result.estimated_cost
                selected = video_result.videos[0] if video_result.videos else None
                external_task_id = video_result.task_id
                generation_status = video_result.status
                raw_response = video_result.raw_response or {}
                if selected:
                    external_task_id = selected.task_id or external_task_id
                    generation_status = selected.status or generation_status
                    result_url = selected.url
                    raw_response = selected.raw_response or raw_response
                last_frame_url = self._last_frame_url_from_raw(raw_response, video_result.raw_response)
                if selected and (selected.url or selected.b64_data):
                    video_bytes, source = self._materialize_generated_video(selected)
                    video_uri = self.media.write_binary_artifact(run_id, video_filename, video_bytes)
                elif external_task_id:
                    source = "external_task_pending"
                    video_uri = self.media.reserve_binary_artifact(run_id, video_filename)
                    self._trace_external_task(
                        runtime_config,
                        event_type="external_task_submitted",
                        message=f"Video generation task submitted: {external_task_id}.",
                        payload={
                            "asset_type": "video",
                            "variant_id": variant_id,
                            "external_task_id": external_task_id,
                            "generation_status": generation_status,
                            "selected_provider_name": provider_used,
                            "selected_model_name": model_used,
                            "duration_seconds": duration_seconds,
                        },
                    )
                else:
                    error_text = "Video generation returned no data, no URL, and no external task ID."
                    video_uri = self._generation_error_artifact(run_id, variant_id, error_text)
                    source = "generation_error"
        except Exception as exc:
            error_text = str(exc)
            provider_errors = getattr(exc, "errors", []) or []
            video_uri = self._generation_error_artifact(run_id, variant_id, error_text)
        asset = VideoAsset(variant_id=variant_id, video_uri=video_uri, duration_seconds=float(duration_seconds))
        payload = {
            **asset.model_dump(),
            "source": source,
            "video_provider": provider_used,
            "video_model": model_used,
            "error": error_text,
            "prompt": video_prompt,
            "external_task_id": external_task_id,
            "generation_status": generation_status,
            "result_url": result_url,
            "last_frame_url": last_frame_url,
            "raw_response": raw_response,
            "provider_errors": provider_errors,
            "quality_constraints": {
                "preserve_submitted_product_identity": True,
                "require_physical_plausibility": True,
            },
            "generation_spec": generation_spec,
            "external_task_state": self._external_task_state(
                asset_type="video",
                task_id=external_task_id,
                status=generation_status,
                provider_name=provider_used,
                model_name=model_used,
                event="submitted",
            ),
        }
        return payload, estimated_cost, model_used

    def run_video_generation(
        self,
        run_id: str,
        script_pack: VideoScriptPack,
        storyboard_frames: list[dict] | None = None,
        *,
        creative_specs: dict | None,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
        on_video_asset=None,
    ) -> StageOutput:
        creative_specs = creative_specs or {}
        generation_spec = {**(script_pack.generation_spec or {}), **self._video_generation_spec(creative_specs)}
        if storyboard_frames:
            frame_urls = []
            for frame in storyboard_frames:
                provider_url = self._storyboard_frame_reference_url(frame)
                if provider_url:
                    frame_urls.append(provider_url)
                    continue
                uri = frame.get("image_uri")
                if not uri:
                    continue
                data_url = self._local_image_to_data_url(uri)
                if data_url:
                    frame_urls.append(data_url)
            if frame_urls:
                existing = list(generation_spec.get("image_urls") or [])
                generation_spec["image_urls"] = existing + frame_urls
        product_context = script_pack.product_context or {}
        video_size = str(generation_spec.get("size") or creative_specs.get("video_size") or "9:16")
        resolution = str(generation_spec.get("resolution") or creative_specs.get("resolution") or "720p")
        duration_seconds = int(generation_spec.get("duration") or creative_specs.get("video_duration_seconds") or 8)
        prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="Video Generation Agent",
            task_instruction=(
                f"Generate videos from script pack: {script_pack.model_dump()}. "
                f"product_context={product_context}. generation_spec={generation_spec}. "
                "Return ONLY valid JSON shaped as {\"video_prompts\":[{\"variant_id\":\"V1\",\"prompt\":\"...\"}]}."
            ),
        )
        try:
            response_text, text_model_used, estimated_cost = self._chat_complete(provider, model, prompt, runtime_config)
        except Exception:
            response_text = ""
            text_model_used = f"{model}:fallback_to_template"
            estimated_cost = 0.0
        llm_video_prompts: dict[str, dict] = {}
        try:
            parsed = self._parse_llm_json(response_text, schema_key="video_prompts")
            for prompt_data in parsed["video_prompts"]:
                llm_video_prompts[prompt_data["variant_id"]] = prompt_data
        except (ValueError, KeyError, TypeError):
            if not text_model_used.endswith(":fallback_to_template"):
                text_model_used = text_model_used + ":fallback_to_template"
        videos: list[VideoAsset] = []
        artifacts: list[dict] = []
        video_models_used: set[str] = set()
        for script in script_pack.scripts:
            script.segments = self._apply_segment_contracts(
                script.segments,
                product_truth=dict(product_context.get("product_truth_contract") or {}),
                product_name=str(product_context.get("product_name") or "the product"),
            )
            tiktok_details = script.tiktok.model_dump() if script.tiktok else {}
            tiktok_line = ""
            if tiktok_details:
                tiktok_line = (
                    f"TikTok Shop style={tiktok_details.get('style')}; "
                    f"opening_hook={tiktok_details.get('opening_hook')}; "
                    f"on_screen_text={tiktok_details.get('on_screen_text')}; "
                    f"cta={tiktok_details.get('cta')}. "
                )
            llm_prompt_data = llm_video_prompts.get(script.variant_id)
            if llm_prompt_data is not None:
                video_prompt = llm_prompt_data["prompt"]
            else:
                video_prompt = (
                    "Generate a short social ad video clip based on script. "
                    f"Hook: {script.hook}. Script: {script.script}. Shots: {script.shot_list}. "
                    f"{tiktok_line}"
                    f"Output should be brand-safe and product-forward, aspect ratio {video_size}, "
                    f"target resolution {resolution}, duration {duration_seconds} seconds. "
                    f"{self._video_prompt_quality_block(product_context)}"
                )
            video_prompt += self._qa_repair_prompt_block(runtime_config)
            asset_suffix = str((runtime_config or {}).get("asset_name_suffix") or "")
            force_regenerate = bool((runtime_config or {}).get("force_regenerate"))
            if script.segments:
                segment_queue = [segment.model_dump() for segment in script.segments]
                resume_index, segment_payloads, completed_segment_paths, bridge_frame_uri = self._segment_resume_state(
                    segments=script.segments,
                    resume_payload=(runtime_config or {}).get("resume_video_payload"),
                )
                base_image_refs = list(generation_spec.get("image_urls") or [])
                role_image_refs = list(generation_spec.get("image_with_roles") or [])
                try:
                    max_reference_images = int(generation_spec.get("max_reference_images") or 9)
                except (TypeError, ValueError):
                    max_reference_images = 9
                allow_data_url_references = not self._video_runtime_requires_hosted_references(
                    fallback_provider=provider,
                    fallback_model=model,
                    runtime_config=runtime_config,
                )
                for segment_index, segment in enumerate(script.segments[resume_index:], start=resume_index):
                    segment_duration = int(segment.duration_seconds)
                    segment_spec = {**generation_spec, "duration": segment_duration, "return_last_frame": True}
                    segment_spec.pop("image_urls", None)
                    segment_spec.pop("image_with_roles", None)
                    if segment_index == 0 and role_image_refs:
                        segment_reference_payload, reference_mode = {"image_with_roles": role_image_refs}, "role_refs"
                        reference_manifest = [
                            {
                                "uri": str(item.get("url") or ""),
                                "source": "role_ref",
                                "role": str(item.get("role") or "reference"),
                                "provider_usable": True,
                                "used": True,
                                "transport": "hosted_url",
                            }
                            for item in role_image_refs
                            if isinstance(item, dict) and item.get("url")
                        ]
                    else:
                        segment_reference_payload, reference_mode, reference_manifest = self._segment_image_reference_payload(
                            base_image_refs,
                            bridge_frame_uri,
                            max_reference_images=max_reference_images,
                            allow_data_urls=allow_data_url_references,
                        )
                    segment_spec.update(segment_reference_payload)
                    reference_instruction = ""
                    if reference_mode == "first_frame":
                        reference_instruction = (
                            "Reference usage: first_frame is the previous segment tail; maintain the same model and product identity. "
                        )
                    elif reference_mode == "role_refs":
                        reference_instruction = "Reference usage: use the supplied first_frame/last_frame role references exactly as temporal anchors. "
                    elif reference_mode == "tail_with_anchors":
                        reference_instruction = (
                            "Reference usage: image 1 is the previous segment tail frame; remaining images are product/model anchors. "
                        )
                    elif reference_mode == "anchors":
                        reference_instruction = "Reference usage: images are product/model identity anchors. "
                    segment_prompt = (
                        f"{video_prompt}\n\nSegment {segment.segment_id}: {segment.motion_prompt}. "
                        f"Bridge rule: {self._segment_bridge_instruction(segment_index, bool(bridge_frame_uri))} "
                        f"{reference_instruction}"
                        f"First frame: {segment.first_frame_prompt}. Last frame target: {segment.last_frame_prompt}. "
                        f"Continuity constraints: {segment.continuity_constraints}. "
                        f"Segment contract: {segment.segment_contract}. "
                        f"Transition to next: {segment.transition_to_next}."
                    )
                    segment_prompt += self._human_integrity_instruction(segment_prompt)
                    segment_payload, segment_cost, segment_model = self._generate_video_clip_payload(
                        run_id=run_id,
                        variant_id=script.variant_id,
                        video_prompt=segment_prompt,
                        video_size=video_size,
                        resolution=resolution,
                        duration_seconds=segment_duration,
                        generation_spec=segment_spec,
                        provider=provider,
                        model=model,
                        runtime_config=runtime_config,
                        video_filename=f"{segment.segment_id}{asset_suffix}.mp4",
                        force_regenerate=force_regenerate,
                    )
                    estimated_cost += segment_cost
                    if segment_model:
                        video_models_used.add(segment_model)
                    segment_payload["segment_id"] = segment.segment_id
                    segment_payload["segment_index"] = segment_index
                    segment_payload["transition_to_next"] = segment.transition_to_next
                    segment_payload["segment_contract"] = segment.segment_contract
                    segment_payload["reference_mode"] = reference_mode
                    segment_payload["reference_image_count"] = len(segment_spec.get("image_urls") or []) + len(segment_spec.get("image_with_roles") or [])
                    segment_payload["reference_manifest"] = reference_manifest
                    segment_payload["hosted_reference_ready"] = all(
                        bool(item.get("provider_usable")) for item in reference_manifest if item.get("used")
                    )
                    status = str(segment_payload.get("generation_status") or "").lower()
                    if status in {"completed", "succeeded", "success", "ready"} and self._artifact_has_payload(segment_payload.get("video_uri")):
                        segment_path = Path(str(segment_payload["video_uri"]))
                        completed_segment_paths.append(segment_path)
                        bridge_frame_uri = extract_last_video_frame(
                            video_path=segment_path,
                            output_path=self.media.settings.assets_dir / run_id / f"{segment.segment_id}_last_frame.png",
                        )
                        segment_payload["last_frame_uri"] = bridge_frame_uri
                        if segment_payload.get("last_frame_url"):
                            bridge_frame_uri = str(segment_payload["last_frame_url"])
                        self._attach_segment_frame_qa(run_id=run_id, segment_payload=segment_payload)
                    segment_payloads.append(segment_payload)
                    if not bridge_frame_uri or (segment_payload.get("segment_frame_qa") or {}).get("blocking"):
                        break

                stitched_uri = None
                for segment_payload in segment_payloads:
                    self._attach_segment_frame_qa(run_id=run_id, segment_payload=segment_payload)
                stitch_preflight = self._stitch_preflight(segments=script.segments, segment_payloads=segment_payloads)
                if len(completed_segment_paths) == len(script.segments) and stitch_preflight["status"] == "pass":
                    stitched_uri = stitch_video_files(
                        video_paths=completed_segment_paths,
                        output_path=self.media.settings.assets_dir / run_id / f"{script.variant_id}_stitched{asset_suffix}.mp4",
                    )
                segment_ledger = self._segment_ledger(
                    variant_id=script.variant_id,
                    segment_queue=segment_queue,
                    segment_payloads=segment_payloads,
                    stitch_preflight=stitch_preflight,
                )
                video_payload = {
                    "variant_id": script.variant_id,
                    "video_uri": stitched_uri or (segment_payloads[-1]["video_uri"] if segment_payloads else ""),
                    "duration_seconds": float(sum(segment.duration_seconds for segment in script.segments)),
                    "source": "stitched_segments" if stitched_uri else "segmented_pending",
                    "generation_status": "completed" if stitched_uri else "pending",
                    "segments": segment_payloads,
                    "segment_queue": segment_queue,
                    "segment_ledger": segment_ledger,
                    "stitch_preflight": stitch_preflight,
                    "segment_prompt_base": video_prompt,
                    "asset_suffix": asset_suffix,
                    "video_size": video_size,
                    "resolution": resolution,
                    "generation_spec": generation_spec,
                    "quality_constraints": {
                        "preserve_submitted_product_identity": True,
                        "require_physical_plausibility": True,
                    },
                }
                if not stitched_uri and segment_payloads and segment_payloads[-1].get("error"):
                    video_payload["source"] = segment_payloads[-1].get("source") or "placeholder"
                    video_payload["generation_status"] = "failed"
                    video_payload["error"] = segment_payloads[-1].get("error")
            else:
                clip_generation_spec = generation_spec
                clip_prompt = video_prompt
                if generation_spec.get("image_urls"):
                    try:
                        max_reference_images = int(generation_spec.get("max_reference_images") or 9)
                    except (TypeError, ValueError):
                        max_reference_images = 9
                    reference_payload, reference_mode, _reference_manifest = self._segment_image_reference_payload(
                        list(generation_spec.get("image_urls") or []),
                        None,
                        max_reference_images=max_reference_images,
                    )
                    clip_generation_spec = {**generation_spec}
                    clip_generation_spec.pop("image_urls", None)
                    clip_generation_spec.update(reference_payload)
                clip_prompt += self._human_integrity_instruction(clip_prompt)
                video_payload, clip_cost, clip_model = self._generate_video_clip_payload(
                    run_id=run_id,
                    variant_id=script.variant_id,
                    video_prompt=clip_prompt,
                    video_size=video_size,
                    resolution=resolution,
                    duration_seconds=duration_seconds,
                    generation_spec=clip_generation_spec,
                    provider=provider,
                    model=model,
                    runtime_config=runtime_config,
                    video_filename=f"{script.variant_id}_sample{asset_suffix}.mp4",
                    force_regenerate=force_regenerate,
                )
                estimated_cost += clip_cost
                if clip_model:
                    video_models_used.add(clip_model)
            video_uri = str(video_payload.get("video_uri") or "")
            video_payload = self._attach_generated_video_frames(run_id=run_id, video_payload=video_payload)
            video_payload["visual_qa"] = self._local_media_qa(
                asset_type="video",
                uri=video_uri,
                payload=video_payload,
                expected_ratio=video_size,
            )
            videos.append(VideoAsset.model_validate(video_payload))
            artifacts.append(
                {
                    "type": "generated_video",
                    "uri": video_uri,
                    "payload": video_payload,
                }
            )
            if on_video_asset:
                on_video_asset(video_payload)
        bundle = VideoBundle(videos=videos)
        bundle_payload = {
            "videos": [artifact["payload"] for artifact in artifacts if artifact["type"] == "generated_video"],
            "strategy_handoff": self._business_strategy_handoff(
                stage="video_generation",
                decisions=[f"submitted/generated video assets for {len(videos)} variants", "stored provider task metadata for recovery"],
                risks=["Pending async videos require refresh before final visual QA can pass."],
                review_questions=["Did the completed video preserve product logic?", "Should broken continuity variants be regenerated?"],
            ),
        }
        uri = self.media.write_text_artifact(run_id, "video_bundle.json", bundle.model_dump_json(indent=2))
        artifacts.append({"type": "video_bundle", "uri": uri, "payload": bundle_payload})
        final_model_used = f"text={text_model_used};video={','.join(sorted(m for m in video_models_used if m)) or 'placeholder'}"
        return StageOutput(
            payload=bundle_payload,
            model_used=final_model_used,
            estimated_cost=estimated_cost,
            artifacts=artifacts,
        )

    def run_visual_quality_assessment(
        self,
        run_id: str,
        variant_set: VariantSet,
        *,
        copy_images: dict | None = None,
        video_scripts: dict | None = None,
        storyboards: dict | None = None,
        videos: dict | None = None,
        intake: dict | None = None,
        business_context: dict | None = None,
        creative_specs: dict | None = None,
        social_review_contract: dict | None = None,
        gm_policy: dict | None = None,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
    ) -> StageOutput:
        copy_images = copy_images or {}
        video_scripts = video_scripts or {}
        storyboards = storyboards or {}
        videos = videos or {}
        intake = intake or {}
        business_context = business_context or {}
        creative_specs = creative_specs or {}
        social_review_contract = social_review_contract or {}
        gm_policy = gm_policy or {}
        marketplace_goal = is_marketplace_main_image(creative_specs)
        review_hints = get_dtc_site_review_hints(creative_specs)
        visual_identity = dict(intake.get("visual_identity") or {}) if isinstance(intake, dict) else {}
        product_truth = dict(intake.get("product_truth_contract") or {}) if isinstance(intake, dict) else {}
        if not product_truth and visual_identity:
            product_truth = build_product_truth_contract(
                product_name=str(intake.get("product_name") or "the product"),
                visual_identity=visual_identity,
                sku_summary=list(intake.get("sku_summary") or []),
            )

        def _asset_items(payload: dict, key: str) -> list[dict]:
            rows = payload.get(key) or []
            return [dict(item) for item in rows if isinstance(item, dict)]

        asset_rows: list[dict] = []
        for image in _asset_items(copy_images, "image_assets"):
            asset_rows.append({"asset_type": "image", **image})
        for frame in _asset_items(storyboards, "frames"):
            asset_rows.append({"asset_type": "storyboard_frame", **frame, "uri": frame.get("image_uri")})
        for video in _asset_items(videos, "videos"):
            asset_rows.append({"asset_type": "video", **video, "uri": video.get("video_uri")})

        scripts_by_variant = {item.get("variant_id"): item for item in _asset_items(video_scripts, "scripts")}

        # Extract shot_plan summaries for QA context
        shot_plan_by_variant: dict[str, list[dict]] = {}
        for item in _asset_items(video_scripts, "scripts"):
            vid = item.get("variant_id")
            sp = item.get("shot_plan") or []
            if vid and sp:
                shot_plan_by_variant[vid] = [
                    {
                        "shot_id": s.get("shot_id", ""),
                        "intent": s.get("intent", ""),
                        "duration": s.get("duration_seconds"),
                        "constraints": s.get("product_continuity_constraints", []),
                    }
                    for s in sp
                ]

        reports: list[dict] = []
        summaries: list[dict] = []
        model_image_inputs: list[str] = []
        model_video_inputs: list[str] = []
        model_media_manifest: list[dict] = []

        def _media_url_for_model(asset: dict, *, media_type: str) -> str | None:
            uri = asset.get("uri")
            if not isinstance(uri, str) or not uri:
                return None
            if uri.startswith(("http://", "https://", "data:")):
                return uri
            if media_type == "video":
                return self._local_video_to_data_url(uri)
            extra = (runtime_config or {}).get("extra") or {}
            return self._local_image_to_review_data_url(
                uri,
                max_side=int(extra.get("visual_qa_image_max_side") or 1024),
                quality=int(extra.get("visual_qa_image_jpeg_quality") or 82),
            )

        max_model_images = int(((runtime_config or {}).get("extra") or {}).get("visual_qa_max_model_images") or 12)
        max_model_videos = int(((runtime_config or {}).get("extra") or {}).get("visual_qa_max_model_videos") or 4)

        for variant in variant_set.variants:
            variant_assets = [item for item in asset_rows if item.get("variant_id") == variant.variant_id]
            visual_proof_spec = self._variant_visual_proof_spec(variant)
            asset_reports: list[dict] = []
            blocking_issues: list[str] = []
            platform_readiness: dict[str, str] = {}
            score_values: list[float] = []
            pending = False
            warn = False
            export_ready_assets = 0
            for asset in variant_assets:
                asset_type = str(asset.get("asset_type") or "")
                expected_ratio = None
                if asset_type in {"image", "storyboard_frame"}:
                    expected_ratio = asset.get("aspect_ratio") or (creative_specs.get("image_size") if isinstance(creative_specs, dict) else None)
                qa = asset.get("visual_qa")
                if not isinstance(qa, dict):
                    qa = self._local_media_qa(
                        asset_type=asset_type,
                        uri=asset.get("uri"),
                        payload=asset,
                        expected_ratio=expected_ratio,
                    )
                if asset_type == "video" and str(asset.get("generation_status") or "").lower() in {
                    "completed",
                    "succeeded",
                    "success",
                    "ready",
                }:
                    frame_uris = [str(uri) for uri in (asset.get("frame_uris") or []) if str(uri).strip()]
                    if not frame_uris:
                        frame_uris = [
                            str(frame.get("uri"))
                            for frame in (asset.get("generated_video_frames") or [])
                            if isinstance(frame, dict) and str(frame.get("uri") or "").strip()
                        ]
                    frame_review = inspect_extracted_video_frames(
                        frame_uris=frame_uris,
                        social_review_contract=social_review_contract,
                        shot_plan=shot_plan_by_variant.get(variant.variant_id) or [],
                    )
                    qa = self._merge_video_frame_review(qa=qa, frame_review=frame_review)
                flags = qa.get("flags") or []
                status = str(qa.get("status") or "warn")
                image_asset_contract = asset.get("image_asset_contract") if isinstance(asset.get("image_asset_contract"), dict) else {}
                if asset_type in {"image", "storyboard_frame"} and image_asset_contract:
                    contract_flags = [str(flag) for flag in (image_asset_contract.get("flags") or [])]
                    flags = sorted(set([*flags, *contract_flags]))
                    if image_asset_contract.get("blocking") or image_asset_contract.get("status") == "fail":
                        status = "fail"
                        blocking_issues.extend(contract_flags or ["image_asset_contract_failed"])
                    elif image_asset_contract.get("status") == "warn":
                        warn = True
                if isinstance(qa.get("score"), (int, float)):
                    score_values.append(float(qa["score"]))
                if status == "fail":
                    blocking_issues.extend(str(flag) for flag in flags)
                marketplace_qa = asset.get("marketplace_qa")
                if marketplace_goal and asset_type == "image":
                    if not isinstance(marketplace_qa, dict):
                        marketplace_qa = inspect_marketplace_image(
                            uri=asset.get("uri"),
                            payload=asset,
                            creative_specs=creative_specs,
                            visual_identity=visual_identity,
                        )
                        asset["marketplace_qa"] = marketplace_qa
                    market_status = str(marketplace_qa.get("status") or "warn")
                    market_flags = [str(flag) for flag in (marketplace_qa.get("flags") or [])]
                    flags = sorted(set([*flags, *market_flags]))
                    if isinstance(marketplace_qa.get("score"), (int, float)):
                        score_values.append(float(marketplace_qa["score"]))
                    if market_status == "fail":
                        blocking_issues.extend(market_flags or ["marketplace_qa_failed"])
                    if market_status == "warn":
                        warn = True
                    if marketplace_qa.get("export_ready"):
                        export_ready_assets += 1
                    for platform, readiness in (marketplace_qa.get("platform_readiness") or {}).items():
                        current = platform_readiness.get(platform)
                        if current == "fail" or readiness == "fail":
                            platform_readiness[platform] = "fail"
                        elif current == "warn" or readiness == "warn":
                            platform_readiness[platform] = "warn"
                        else:
                            platform_readiness[platform] = str(readiness)
                if "visual_qa_video_processing" in flags or str(asset.get("generation_status") or "").lower() in {"submitted", "queued", "pending", "processing", "running"}:
                    pending = True
                stitch_preflight = asset.get("stitch_preflight") or {}
                if asset_type == "video" and isinstance(stitch_preflight, dict) and stitch_preflight.get("status") == "fail":
                    stitch_flags = [str(flag) for flag in (stitch_preflight.get("flags") or [])]
                    flags = sorted(set([*flags, *(stitch_flags or ["stitch_preflight_failed"])]))
                    blocking_issues.extend(stitch_flags or ["stitch_preflight_failed"])
                if status == "warn":
                    warn = True
                if "visual_qa_needs_frame_review" in flags:
                    warn = True
                if asset_type == "video" and self._mentions_human_subject(
                    " ".join(
                        str(item)
                        for item in [
                            asset.get("prompt"),
                            asset.get("segment_prompt_base"),
                            variant.hook,
                            variant.message,
                            scripts_by_variant.get(variant.variant_id),
                        ]
                    )
                ):
                    flags = sorted(set([*flags, "visual_qa_human_anatomy_review"]))
                    warn = True
                if asset_type in {"image", "storyboard_frame"} and len(model_image_inputs) < max_model_images:
                    media_url = _media_url_for_model(asset, media_type="image")
                    if media_url:
                        model_image_inputs.append(media_url)
                        model_media_manifest.append(
                            {
                                "input_index": len(model_media_manifest) + 1,
                                "modality": "image",
                                "variant_id": variant.variant_id,
                                "asset_type": asset_type,
                                "uri": asset.get("uri"),
                                "review_transport": "compressed_data_url" if media_url.startswith("data:image/jpeg") else "original_uri",
                                "review_input_chars": len(media_url),
                            }
                        )
                if asset_type == "video" and len(model_video_inputs) < max_model_videos:
                    media_url = _media_url_for_model(asset, media_type="video")
                    if media_url:
                        model_video_inputs.append(media_url)
                        model_media_manifest.append(
                            {
                                "input_index": len(model_media_manifest) + 1,
                                "modality": "video",
                                "variant_id": variant.variant_id,
                                "asset_type": asset_type,
                                "uri": asset.get("uri"),
                                "generation_status": asset.get("generation_status"),
                            }
                        )
                asset_reports.append(
                    {
                        "asset_type": asset_type,
                        "uri": asset.get("uri"),
                        "generation_status": asset.get("generation_status"),
                        "external_task_id": asset.get("external_task_id"),
                        "qa_status": status,
                        "visual_score": qa.get("score"),
                        "frame_uris": asset.get("frame_uris") or [],
                        "flags": flags,
                        "checks": qa.get("checks") or [],
                        "stitch_preflight": stitch_preflight if asset_type == "video" else None,
                        "image_asset_contract": image_asset_contract if asset_type in {"image", "storyboard_frame"} else None,
                        "marketplace_qa": marketplace_qa if marketplace_goal and asset_type == "image" else None,
                        "visual_proof_spec": asset.get("visual_proof_spec") or visual_proof_spec,
                    }
                )

            if not variant_assets:
                blocking_issues.append("visual_qa_no_generated_assets")
            visual_score = min(score_values) if score_values else 0.0
            if pending:
                qa_status = "pending"
                recommended_action = "wait_for_asset"
            elif blocking_issues:
                qa_status = "fail"
                recommended_action = "request_regeneration"
            elif marketplace_goal and variant_assets and export_ready_assets <= 0:
                qa_status = "warn"
                recommended_action = "manual_review"
            elif warn or visual_score < 80:
                qa_status = "warn"
                recommended_action = "manual_review"
            else:
                qa_status = "pass"
                recommended_action = "pass_to_evaluation"
            export_ready = marketplace_goal and not pending and not blocking_issues and bool(platform_readiness) and all(
                value == "pass" for value in platform_readiness.values()
            )

            script = scripts_by_variant.get(variant.variant_id) or {}
            report = {
                "variant_id": variant.variant_id,
                "angle": variant.angle,
                "hook": variant.hook,
                "qa_status": qa_status,
                "visual_score": round(visual_score, 2),
                "asset_reports": asset_reports,
                "blocking_issues": sorted(set(blocking_issues)),
                "review_notes": (
                    f"{variant.variant_id} visual QA {qa_status}; "
                    f"{len(asset_reports)} assets checked; action={recommended_action}."
                ),
                "recommended_action": recommended_action,
                "platform_readiness": platform_readiness,
                "export_ready": export_ready,
                "script_hook": script.get("hook"),
                "review_hints": review_hints,
                "product_truth_contract": product_truth,
                "visual_proof_spec": visual_proof_spec,
            }
            reports.append(report)
            product_truth_flags = sorted(
                {
                    str(flag)
                    for asset_report in asset_reports
                    for flag in (asset_report.get("flags") or [])
                    if str(flag).startswith("visual_qa_product_truth")
                }
            )
            summaries.append(
                {
                    "variant_id": variant.variant_id,
                    "qa_status": qa_status,
                    "visual_score": report["visual_score"],
                    "blocking_issue_count": len(report["blocking_issues"]),
                    "recommended_action": recommended_action,
                    "issues": sorted(set([*report["blocking_issues"], *product_truth_flags])),
                    "product_truth_flags": product_truth_flags,
                    "frame_review_flags": sorted(
                        {
                            str(flag)
                            for asset_report in asset_reports
                            for flag in (asset_report.get("flags") or [])
                            if "frame" in str(flag)
                        }
                    ),
                    "platform_readiness": platform_readiness,
                    "export_ready": export_ready,
                    "review_hints": review_hints,
                    "visual_proof_spec": visual_proof_spec,
                }
            )

        prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="Visual QA Agent",
            task_instruction=(
                "Review these structured visual QA records only for visual quality and product truth. "
                "Focus on product fidelity, physical plausibility, channel fit, and whether any candidate is visually unsafe to evaluate. "
                "Do not make claim-safety, policy, legal, or platform-compliance decisions; leave those to Evaluation. "
                "When product_truth_flags are present, compare attached media against the product_truth_contract before passing the candidate. "
                "When visual_proof_spec is present, check whether the media proves the desired_scene/proof_mechanism and avoids semantic_fail_conditions. "
                "For inspected still images judge the intended placement: can the viewer identify the product and see the proof at the intended crop? "
                "Check any requested copy area for collisions. Identify distracting props or effects only when they obscure that proof or conflict with the brief. "
                "Describe the visible region and its impact in evidence. For a failed still image give one concrete repair_instruction naming the region and edit. "
                "Leave it empty for other assets or when pixels cannot be inspected. Style preference alone is not a failure. "
                "Return ONLY valid JSON shaped as {\"model_summary\":\"...\",\"visual_proof_reviews\":[{\"variant_id\":\"V1\",\"status\":\"pass|warn|fail\",\"visual_score\":0-100,\"evidence\":\"...\",\"failed_conditions\":[\"...\"],\"repair_instruction\":\"\"}]}. "
                "Use status=fail when the media expresses a semantic_fail_condition. Do not choose the final winner.\n"
                f"intake_facts={json.dumps(intake, ensure_ascii=False)[:3000]}\n"
                f"product_truth_contract={json.dumps(product_truth, ensure_ascii=False)[:2000]}\n"
                f"business_context={json.dumps(business_context, ensure_ascii=False)[:1800]}\n"
                f"qa_records={json.dumps(summaries, ensure_ascii=False)[:5000]}\n"
                f"attached_media_manifest={json.dumps(model_media_manifest, ensure_ascii=False)[:3000]}\n"
                f"gm_policy={json.dumps(gm_policy.get('stage_guidance') or {}, ensure_ascii=False)[:2000]}\n"
                f"shot_plan_contracts={json.dumps(shot_plan_by_variant, ensure_ascii=False)[:2000]}"
                "Additional checks: verify product appears clearly in early frames per shot intent, "
                "visual continuity matches product_continuity_constraints (color, material, scale), "
                "and each frame adheres to its shot intent."
            ),
        )
        model_summary = ""
        model_used = model
        estimated_cost = 0.0
        review_runtime_config = dict(runtime_config or {})
        review_extra = dict(review_runtime_config.get("extra") or {})
        current_timeout = int(review_runtime_config.get("request_timeout_seconds") or review_extra.get("request_timeout_seconds") or 0)
        review_extra["request_timeout_seconds"] = max(current_timeout, 180)
        review_runtime_config["request_timeout_seconds"] = review_extra["request_timeout_seconds"]
        if provider == "kimi" or model.startswith("kimi-k"):
            review_extra["thinking_mode"] = "disabled"
            review_runtime_config["thinking_mode"] = "disabled"
            current_max_tokens = int(review_runtime_config.get("max_output_tokens") or review_extra.get("max_output_tokens") or 0)
            review_extra["max_output_tokens"] = max(current_max_tokens, 2400)
            review_runtime_config["max_output_tokens"] = review_extra["max_output_tokens"]
        review_runtime_config["extra"] = review_extra
        try:
            model_summary, model_used, estimated_cost = self._chat_complete(
                provider,
                model,
                prompt,
                review_runtime_config,
                image_urls=model_image_inputs,
                video_urls=model_video_inputs,
            )
        except Exception as exc:
            model_summary = f"model_review_unavailable: {str(exc)[:240]}"
            self._mark_visual_model_review_unavailable(reports=reports, summaries=summaries)
        else:
            try:
                model_summary = self._apply_visual_proof_reviews(
                    reports=reports,
                    summaries=summaries,
                    model_summary=model_summary,
                )
            except ValueError as exc:
                model_summary = f"model_review_unavailable: parse_error: {str(exc)[:200]}"
                self._mark_visual_model_review_unavailable(reports=reports, summaries=summaries)

        payload = {
            "reports": reports,
            "variant_summaries": summaries,
            "model_summary": model_summary,
            "model_review_status": "unavailable" if model_summary.startswith("model_review_unavailable:") else "completed",
            "model_media_inputs": {
                "image_count": len(model_image_inputs),
                "video_count": len(model_video_inputs),
                "manifest": model_media_manifest,
            },
            "active_gm_policy": gm_policy,
            "strategy_handoff": self._business_strategy_handoff(
                stage="visual_quality_assessment",
                decisions=[f"checked visual/product-truth quality for {len(summaries)} variants", "blocked or downgraded incomplete and visually risky assets before evaluation"],
                risks=["Large videos may be skipped from model media input and require async refresh or frame sampling.", "Claim-safety and policy risk are deferred to Evaluation compliance_block."],
                review_questions=["Which variants need human frame review?", "Should failed assets be regenerated before ranking?"],
            ),
        }
        uri = self.media.write_text_artifact(run_id, "visual_quality_assessment.json", json.dumps(payload, ensure_ascii=False, indent=2))
        return StageOutput(
            payload=payload,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=[{"type": "visual_quality_report", "uri": uri, "payload": payload}],
        )

    def _parse_evaluation_json(self, raw: str) -> dict:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', raw)
            if not match:
                raise ValueError("Evaluation response did not contain a JSON object")
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("Evaluation response JSON is not an object")
        variants = parsed.get("variants")
        if not isinstance(variants, list) or not variants:
            raise ValueError("Evaluation response missing non-empty variants array")
        return parsed

    def _require_evaluation_scores(self, llm: dict, *, variant_id: str, dim_keys: list[str]) -> None:
        if not isinstance(llm.get("total_score"), (int, float)):
            raise ValueError(f"Evaluation response missing total_score for {variant_id}")
        if not any(isinstance(llm.get(key), (int, float)) for key in dim_keys):
            raise ValueError(f"Evaluation response missing sub-scores for {variant_id}")

    def _build_evaluation_context(
        self,
        variant_set: VariantSet,
        copy_bundle: CopyImageBundle,
        script_pack: VideoScriptPack,
        video_bundle: VideoBundle,
        visual_quality: dict,
    ) -> list[dict]:
        copy_by_id = {v.variant_id: v for v in copy_bundle.copy_variants}
        image_by_id = {v.variant_id: v for v in copy_bundle.image_assets}
        video_by_id = {v.variant_id: v for v in video_bundle.videos}
        script_by_id = {s.variant_id: s for s in script_pack.scripts}
        vq_by_id = {
            item.get("variant_id"): item
            for item in (visual_quality.get("variant_summaries") or [])
            if isinstance(item, dict) and item.get("variant_id")
        }
        variants: list[dict] = []
        for item in variant_set.variants:
            copy = copy_by_id.get(item.variant_id)
            image = image_by_id.get(item.variant_id)
            video = video_by_id.get(item.variant_id)
            script = script_by_id.get(item.variant_id)
            vq = vq_by_id.get(item.variant_id) or {}
            entry: dict = {
                "variant_id": item.variant_id,
                "angle": item.angle,
                "hook": item.hook,
                "message": item.message,
                "copy": {
                    "primary_text": copy.primary_text if copy else "",
                    "headline": copy.headline if copy else "",
                    "description": copy.description if copy else "",
                    "cta": copy.call_to_action if copy else "",
                },
                "has_image": bool(image and self._artifact_has_payload(image.uri)),
                "has_video": bool(video and self._artifact_has_payload(video.video_uri)),
                "image_uri": image.uri if image else None,
                "video_uri": video.video_uri if video else None,
                "script_hook": script.hook if script else "",
                "script_summary": script.script[:300] if script and script.script else "",
                "visual_qa_status": vq.get("qa_status", "not_run"),
                "visual_qa_score": vq.get("visual_score"),
                "visual_qa_issues": vq.get("issues") or [],
                "visual_qa_recommended_action": vq.get("recommended_action", ""),
                "shot_plan_summary": [
                    {
                        "shot_id": s.shot_id,
                        "intent": s.intent,
                        "duration": s.duration_seconds,
                        "constraints": s.product_continuity_constraints,
                    }
                    for s in script.shot_plan
                ] if script and script.shot_plan else [],
            }
            if script and script.tiktok:
                entry["tiktok"] = {
                    "style": script.tiktok.style,
                    "opening_hook": script.tiktok.opening_hook,
                    "on_screen_text": script.tiktok.on_screen_text,
                    "voiceover_lines": script.tiktok.voiceover_lines,
                    "shot_timing": [s.model_dump() for s in script.tiktok.shot_timing],
                    "product_proof_points": script.tiktok.product_proof_points,
                    "cta": script.tiktok.cta,
                }
            variants.append(entry)
        return variants

    def _apply_evaluation_gates(
        self,
        llm_scores: dict,
        variant_context: dict,
    ) -> tuple[float, str, list[str], list[str]]:
        """Apply deterministic hard gates on top of LLM scores.

        Returns (capped_total, recommended_action, compliance_risks, compliance_reasons).
        """
        has_media = variant_context.get("has_image") or variant_context.get("has_video")
        qa_status = str(variant_context.get("visual_qa_status") or "")
        qa_action = str(variant_context.get("visual_qa_recommended_action") or "")
        qa_issues = [str(i) for i in (variant_context.get("visual_qa_issues") or [])]

        total = float(llm_scores.get("total_score", 50))
        action = llm_scores.get("recommended_action", "manual_review")
        risks = list(llm_scores.get("compliance_risks") or [])
        reasons = list(llm_scores.get("compliance_reasons") or [])

        # Gate 1: no valid media at all → force regeneration
        if not has_media:
            return min(total, 49.0), "request_regeneration", risks, reasons + ["No valid generated media asset found."]

        # Gate 2: visual QA hard failures
        if qa_status == "fail" or qa_action == "request_regeneration":
            return min(total, 49.0), "request_regeneration", risks, reasons + [f"Visual QA failed: {qa_issues}"]

        # Gate 3: pending async video
        if qa_status == "pending" or qa_action == "wait_for_asset":
            return min(total, 59.0), "manual_review", risks, reasons + ["Video generation still in progress."]

        # Gate 4: model visual review was required but unavailable
        if "visual_qa_model_review_unavailable" in qa_issues:
            return min(total, 59.0), "manual_review", risks, reasons + ["Visual QA model review unavailable."]

        return total, action, risks, reasons

    def _evaluation_compliance_block(self, llm_scores: dict) -> dict:
        block = llm_scores.get("compliance_block")
        if not isinstance(block, dict):
            block = {}
        risks = block.get("risks")
        reasons = block.get("reasons")
        return {
            "level": str(block.get("level") or llm_scores.get("compliance_level") or "low").lower(),
            "score": block.get("score", llm_scores.get("compliance_safety", llm_scores.get("claim_safety"))),
            "risks": risks if isinstance(risks, list) else list(llm_scores.get("compliance_risks") or []),
            "reasons": reasons if isinstance(reasons, list) else list(llm_scores.get("compliance_reasons") or []),
            "recommendation": str(block.get("recommendation") or llm_scores.get("recommended_action") or "manual_review"),
        }

    def run_evaluation_selection(
        self,
        run_id: str,
        variant_set: VariantSet,
        copy_bundle: CopyImageBundle,
        script_pack: VideoScriptPack,
        video_bundle: VideoBundle,
        visual_quality: dict | None = None,
        *,
        provider: str,
        model: str,
        creative_specs: dict | None = None,
        pipeline_mode: str | None = None,
        gm_policy: dict | None = None,
        runtime_config: dict | None = None,
    ) -> StageOutput:
        creative_specs = creative_specs or {}
        gm_policy = gm_policy or {}
        is_tiktok_shop = pipeline_mode == "tiktok_shop_video"
        visual_quality = visual_quality or {}

        variant_contexts = self._build_evaluation_context(
            variant_set, copy_bundle, script_pack, video_bundle, visual_quality,
        )
        evaluation_runtime_config = dict(runtime_config or {})
        evaluation_extra = dict(evaluation_runtime_config.get("extra") or {})
        current_timeout = int(evaluation_runtime_config.get("request_timeout_seconds") or evaluation_extra.get("request_timeout_seconds") or 0)
        evaluation_extra["request_timeout_seconds"] = max(current_timeout, 180)
        evaluation_runtime_config["request_timeout_seconds"] = evaluation_extra["request_timeout_seconds"]
        if provider == "kimi" or model.startswith("kimi-k"):
            evaluation_extra["thinking_mode"] = "disabled"
            evaluation_runtime_config["thinking_mode"] = "disabled"
            current_max_tokens = int(evaluation_runtime_config.get("max_output_tokens") or evaluation_extra.get("max_output_tokens") or 0)
            evaluation_extra["max_output_tokens"] = max(current_max_tokens, 6000)
            evaluation_runtime_config["max_output_tokens"] = evaluation_extra["max_output_tokens"]
        evaluation_runtime_config["extra"] = evaluation_extra

        model_image_inputs: list[str] = []
        model_media_manifest: list[dict] = []
        image_by_id = {image.variant_id: image for image in copy_bundle.image_assets}
        for item in variant_set.variants:
            image = image_by_id.get(item.variant_id)
            if not image or not image.uri:
                continue
            media_url = image.uri
            if media_url.startswith(("http://", "https://", "data:")):
                review_url = media_url
            else:
                review_url = self._local_image_to_review_data_url(media_url)
            if not review_url:
                continue
            model_image_inputs.append(review_url)
            model_media_manifest.append({
                "variant_id": item.variant_id,
                "source_uri": image.uri,
                "media_index": len(model_image_inputs),
                "review_transport": "compressed_data_url" if review_url.startswith("data:image/jpeg") else "original_uri",
                "review_input_chars": len(review_url),
            })

        dimensions = (
            "thumb_stop_power, product_clarity, purchase_intent, native_tiktok_feel, "
            "watch_through_potential, claim_safety, generation_feasibility"
            if is_tiktok_shop
            else "hook_appeal, copy_clarity, brand_alignment, visual_execution, compliance_safety"
        )
        tiktok_note = (
            f" TikTok Shop video style: {creative_specs.get('tiktok_video_style', 'ugc_demo')}."
            if is_tiktok_shop
            else ""
        )

        prompt = self._compose_stage_prompt(
            runtime_config=runtime_config,
            agent_role="Evaluation Agent",
            task_instruction=(
                f"Evaluate each variant and return a JSON object with a 'variants' array. "
                f"Context — variants: {json.dumps(variant_contexts, ensure_ascii=False)}. "
                f"Attached images map to variants by this manifest: {json.dumps(model_media_manifest, ensure_ascii=False)}. "
                f"GM policy: {json.dumps(gm_policy.get('stage_guidance') or {}, ensure_ascii=False)}.{tiktok_note}\n\n"
                f"For each variant, score these dimensions 0-100: {dimensions}. "
                "Also provide: total_score (0-100), compliance_level ('low'/'medium'/'high'), "
                "recommended_action ('approve_variant'/'manual_review'/'request_regeneration'), and brief_reason (1 sentence). "
                "Include a compliance_block object per variant with level, score, risks, reasons, and recommendation. "
                "Use compliance_block only for claim-safety, visible text/implied claim, prohibited-claim, and policy risk; visual defects belong in visual_execution/visual_qa. "
                "Score based on creative quality, not string length. "
                "A variant with missing or placeholder media should score low on execution. "
                "Return ONLY valid JSON, no markdown wrapping."
            ),
        )
        raw, model_used, estimated_cost = self._chat_complete(
            provider,
            model,
            prompt,
            evaluation_runtime_config,
            image_urls=model_image_inputs,
        )
        parsed = self._parse_evaluation_json(raw)
        llm_variants = {
            v.get("variant_id"): v
            for v in (parsed.get("variants") or [])
            if isinstance(v, dict) and v.get("variant_id")
        }

        ctx_by_id = {c["variant_id"]: c for c in variant_contexts}
        ranked: list[RankedVariant] = []
        for item in variant_set.variants:
            ctx = ctx_by_id.get(item.variant_id, {})
            llm = llm_variants.get(item.variant_id, {})
            total, action, risks, reasons = self._apply_evaluation_gates(llm, ctx)

            dim_keys = (
                ["thumb_stop_power", "product_clarity", "purchase_intent", "native_tiktok_feel",
                 "watch_through_potential", "claim_safety", "generation_feasibility"]
                if is_tiktok_shop
                else ["hook_appeal", "copy_clarity", "brand_alignment", "visual_execution", "compliance_safety"]
            )
            self._require_evaluation_scores(llm, variant_id=item.variant_id, dim_keys=dim_keys)
            sub_scores: dict[str, float] = {}
            for k in dim_keys:
                val = llm.get(k)
                sub_scores[k] = round(float(val), 2) if isinstance(val, (int, float)) else 50.0
            vq_score = ctx.get("visual_qa_score")
            sub_scores["visual_qa"] = round(float(vq_score), 2) if isinstance(vq_score, (int, float)) else 50.0
            compliance_block = self._evaluation_compliance_block(llm)

            level_raw = str(compliance_block.get("level") or "low").lower()
            level = ComplianceLevel.LOW if level_raw == "low" else ComplianceLevel.MEDIUM if level_raw == "medium" else ComplianceLevel.HIGH

            llm_reason = str(llm.get("brief_reason") or "")
            has_media = ctx.get("has_image") or ctx.get("has_video")
            qa_status = str(ctx.get("visual_qa_status") or "not_run")
            sys_reasons = [
                f"visual_qa_agent_status={qa_status}",
                "valid generated media available" if has_media else "generated media missing or placeholder",
            ]
            gate_reasons = [r for r in reasons if r]
            all_reasons = [r for r in ([llm_reason] + sys_reasons + gate_reasons) if r] or [f"angle={item.angle}"]

            ranked.append(RankedVariant(
                variant_id=item.variant_id,
                total_score=total,
                sub_scores=sub_scores,
                compliance_level=level,
                compliance_block=compliance_block,
                reasons=all_reasons,
                compliance_risks=list(compliance_block.get("risks") or risks),
                compliance_reasons=list(compliance_block.get("reasons") or reasons) or ["No major compliance issues detected."],
                recommended_action=action,
            ))

        ranked.sort(key=lambda x: x.total_score, reverse=True)
        top_k = ranked[:3]
        winner = top_k[0] if top_k else None

        copy_by_id = {v.variant_id: v for v in copy_bundle.copy_variants}
        video_by_id = {v.variant_id: v for v in video_bundle.videos}
        winner_copy = copy_by_id.get(winner.variant_id) if winner else None
        winner_images = [x for x in copy_bundle.image_assets if winner and x.variant_id == winner.variant_id]
        winner_video = video_by_id.get(winner.variant_id) if winner else None

        selected = SelectedDeliverables(
            winner_variant_id=winner.variant_id if winner else "N/A",
            copy_variant=winner_copy,
            image_assets=winner_images,
            video_asset=winner_video,
            reasoning=winner.reasons if winner else ["no_winner_generated"],
        )
        scorecard = ScoreCard(
            sub_scores=ScoreBreakdown(
                attraction=winner.sub_scores.get("hook_appeal", winner.sub_scores.get("thumb_stop_power", 50)) if winner else 50,
                clarity=winner.sub_scores.get("copy_clarity", winner.sub_scores.get("product_clarity", 50)) if winner else 50,
                brand_alignment=winner.sub_scores.get("brand_alignment", winner.sub_scores.get("native_tiktok_feel", 50)) if winner else 50,
                compliance=winner.sub_scores.get("compliance_safety", winner.sub_scores.get("claim_safety", 50)) if winner else 50,
                ai_naturalness=winner.sub_scores.get("visual_execution", winner.sub_scores.get("generation_feasibility", 50)) if winner else 50,
            ),
            total_score=winner.total_score if winner else 50,
            risk_labels=[],
            explanation={"selection": "winner chosen by LLM evaluation composite score with deterministic safety gates."},
            compliance_level=winner.compliance_level if winner else ComplianceLevel.MEDIUM,
            ai_artifact_score=winner.sub_scores.get("visual_execution", winner.sub_scores.get("generation_feasibility", 50)) if winner else 50,
        )
        forecast = ConversionForecast(
            score_0_100=scorecard.total_score,
            confidence_0_1=0.7 if scorecard.compliance_level == ComplianceLevel.LOW else 0.35,
            drivers=["hook_appeal", "copy_clarity", "visual_execution", "compliance_safety"],
            recommended_action="approve_for_launch_test" if scorecard.total_score >= 65 else "iterate_new_variants",
        )
        evaluation = EvaluationResult(
            ranked_variants=ranked,
            top_k=top_k,
            winner=winner,
            scorecard=scorecard,
            forecast=forecast,
        )
        payload = {
            "evaluation_result": evaluation.model_dump(),
            "selected_deliverables": selected.model_dump(),
            "variants": variant_set.model_dump(),
            "model_media_inputs": {
                "image_count": len(model_image_inputs),
                "manifest": model_media_manifest,
            },
            "compliance_block": {
                item.variant_id: item.compliance_block
                for item in ranked
            },
            "active_gm_policy": gm_policy,
        }
        uri = self.media.write_text_artifact(run_id, "evaluation_selection.json", str(payload))
        return StageOutput(
            payload=payload,
            model_used=model_used,
            estimated_cost=estimated_cost,
            artifacts=[{"type": "evaluation_selection", "uri": uri, "payload": payload}],
            scorecard=scorecard,
            forecast=forecast,
        )

    def run_shop_profile_analysis(
        self,
        store_url: str,
        description: str,
        *,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
        tavily_api_key: str | None = None,
        firecrawl_api_key: str | None = None,
    ) -> dict:
        """Phase 1: Analyze a store's own positioning, SEO, and product catalog using real web search."""
        from app.search import FirecrawlClient, TavilyClient

        store_content = ""
        tavily_results: dict = {}
        search_errors: list[str] = []
        evidence: list[dict] = []
        source_queries: list[str] = []

        if firecrawl_api_key:
            try:
                fc = FirecrawlClient(api_key=firecrawl_api_key)
                result = fc.scrape(store_url)
                store_content = result.markdown[:8000]
                evidence.append({
                    "source": "firecrawl",
                    "url": store_url,
                    "title": result.title,
                    "summary": result.markdown[:500],
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "status": "ok",
                })
            except Exception as exc:
                search_errors.append(f"firecrawl_scrape: {exc}")
                evidence.append({
                    "source": "firecrawl",
                    "url": store_url,
                    "summary": str(exc),
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "status": "failed",
                })

        if tavily_api_key:
            try:
                tv = TavilyClient(api_key=tavily_api_key)
                search_query = f"{description or store_url} brand positioning reviews target audience"
                source_queries.append(search_query)
                tavily_results = tv.search_raw(search_query, max_results=5)
                for row in tavily_results.get("results") or []:
                    evidence.append({
                        "source": "tavily",
                        "url": row.get("url", ""),
                        "title": row.get("title", ""),
                        "summary": row.get("content", ""),
                        "score": row.get("score"),
                        "query": search_query,
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "status": "ok",
                    })
            except Exception as exc:
                search_errors.append(f"tavily_search: {exc}")

        prompt_parts = [
            f"{self._business_strategy_system_prompt('Research Intelligence Analyst')}",
            f"Research this store: {store_url}",
            f"Operator description: {description or 'None provided'}.",
        ]
        if store_content:
            prompt_parts.append(
                f"SCRAPED STORE CONTENT (from Firecrawl):\n{store_content}\n---"
            )
        if tavily_results:
            prompt_parts.append(
                f"WEB SEARCH RESULTS (from Tavily): {json.dumps(tavily_results, indent=2)}\n---"
            )
        if search_errors:
            prompt_parts.append(
                f"Search errors (partial data): {'; '.join(search_errors)}"
            )
        prompt_parts.append(
            "Produce a STRUCTURED JSON profile: positioning (one-line), target_audience (string), "
            "price_tier (budget/mid/premium), product_categories (list), unique_selling_points (list), "
            "seo_keywords (list of 5-10 search terms), content_gaps (list), "
            "brand_voice (tone and style description). "
            "Return ONLY valid JSON, no markdown wrapping."
        )
        prompt = "\n".join(prompt_parts)

        summary, model_used, estimated_cost = self._chat_complete(provider, model, prompt, runtime_config)
        try:
            profile = json.loads(summary)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', summary)
            profile = json.loads(match.group(0)) if match else {"raw_response": summary}

        return {
            "profile": profile,
            "model_used": model_used,
            "estimated_cost": estimated_cost,
            "evidence": evidence,
            "source_queries": source_queries,
            "search_errors": search_errors if search_errors else None,
        }

    def run_competitor_analysis(
        self,
        store_url: str,
        description: str,
        store_profile: dict,
        *,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
        tavily_api_key: str | None = None,
        firecrawl_api_key: str | None = None,
    ) -> dict:
        """Phase 2: Analyze competitors based on store profile using real web search."""
        from app.search import FirecrawlClient, TavilyClient

        search_errors: list[str] = []
        competitor_search_results: dict = {}
        competitor_pages: list[str] = []
        evidence: list[dict] = []
        source_queries: list[str] = []

        if tavily_api_key:
            try:
                tv = TavilyClient(api_key=tavily_api_key)
                positioning = store_profile.get("positioning", description)
                categories = store_profile.get("product_categories", [])
                cat_str = ", ".join(categories[:3]) if categories else ""
                query = f"competitors similar to {positioning} {cat_str} online store"
                source_queries.append(query)
                competitor_search_results = tv.search_raw(query, max_results=5)
                for r in (competitor_search_results.get("results") or []):
                    url = r.get("url", "")
                    evidence.append({
                        "source": "tavily",
                        "url": url,
                        "title": r.get("title", ""),
                        "summary": r.get("content", ""),
                        "score": r.get("score"),
                        "query": query,
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "status": "ok",
                    })
                    if url and url != store_url:
                        competitor_pages.append(url)
            except Exception as exc:
                search_errors.append(f"tavily_competitor_search: {exc}")

        competitor_content: list[str] = []
        if firecrawl_api_key and competitor_pages:
            try:
                fc = FirecrawlClient(api_key=firecrawl_api_key)
                for comp_url in competitor_pages[:3]:
                    try:
                        result = fc.scrape(comp_url)
                        competitor_content.append(
                            f"URL: {comp_url}\nTITLE: {result.title}\n{result.markdown[:4000]}"
                        )
                        evidence.append({
                            "source": "firecrawl",
                            "url": comp_url,
                            "title": result.title,
                            "summary": result.markdown[:500],
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "status": "ok",
                        })
                    except Exception:
                        competitor_content.append(f"URL: {comp_url}\n[Scrape failed]")
                        evidence.append({
                            "source": "firecrawl",
                            "url": comp_url,
                            "summary": "Scrape failed",
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "status": "failed",
                        })
            except Exception as exc:
                search_errors.append(f"firecrawl_competitor: {exc}")

        prompt_parts = [
            f"{self._business_strategy_system_prompt('Research Intelligence Analyst')}",
            f"Store profile: {json.dumps(store_profile)}",
            f"Store URL: {store_url}",
            f"Operator notes: {description or 'None provided'}.",
        ]
        if competitor_search_results:
            prompt_parts.append(
                f"COMPETITOR SEARCH RESULTS: {json.dumps(competitor_search_results, indent=2)}\n---"
            )
        if competitor_content:
            prompt_parts.append(
                "COMPETITOR PAGE CONTENT:\n" + "\n---\n".join(competitor_content)
            )
        if search_errors:
            prompt_parts.append(f"Search errors (partial data): {'; '.join(search_errors)}")
        prompt_parts.append(
            "Identify 3-5 comparable competitors. For each: positioning, creative/ad style patterns, "
            "pricing approach, differentiation opportunities. "
            "Return Markdown with: ## Competitive Landscape Overview, ## Competitor N (name, URL, analysis), "
            "## Differentiation Opportunities, ## Recommended Creative Angles."
        )
        prompt = "\n".join(prompt_parts)

        summary, model_used, estimated_cost = self._chat_complete(provider, model, prompt, runtime_config)
        return {
            "report": summary,
            "model_used": model_used,
            "estimated_cost": estimated_cost,
            "evidence": evidence,
            "source_queries": source_queries,
            "search_errors": search_errors if search_errors else None,
        }

    def run_audience_pain_point_research(
        self,
        store_url: str,
        description: str,
        store_profile: dict,
        competitor_report: str,
        *,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
        tavily_api_key: str | None = None,
        firecrawl_api_key: str | None = None,
    ) -> dict:
        """Research public reviews and community discussions for audience pain points."""
        from app.search import FirecrawlClient, TavilyClient

        search_errors: list[str] = []
        search_results: list[dict] = []
        community_pages: list[str] = []
        evidence: list[dict] = []
        source_queries: list[str] = []
        target = str(store_profile.get("target_audience") or description or store_url)
        categories = store_profile.get("product_categories") or []
        cat_str = ", ".join(str(item) for item in categories[:3])
        queries = [
            f"{target} {cat_str} customer reviews complaints pain points",
            f"{target} {cat_str} reddit forum problems objections",
            f"{description or store_url} reviews negative feedback alternatives",
        ]

        if tavily_api_key:
            try:
                tv = TavilyClient(api_key=tavily_api_key)
                for query in queries:
                    source_queries.append(query)
                    raw = tv.search_raw(query, max_results=4)
                    search_results.append(raw)
                    for row in raw.get("results") or []:
                        url = row.get("url", "")
                        evidence.append({
                            "source": "tavily",
                            "url": url,
                            "title": row.get("title", ""),
                            "summary": row.get("content", ""),
                            "score": row.get("score"),
                            "query": query,
                            "evidence_category": "review_community",
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "status": "ok",
                        })
                        if url and any(token in url.lower() for token in ["reddit", "forum", "review", "amazon", "walmart", "trustpilot"]):
                            community_pages.append(url)
            except Exception as exc:
                search_errors.append(f"tavily_audience_search: {exc}")

        community_content: list[str] = []
        if firecrawl_api_key and community_pages:
            try:
                fc = FirecrawlClient(api_key=firecrawl_api_key)
                for page_url in community_pages[:3]:
                    try:
                        result = fc.scrape(page_url)
                        community_content.append(f"URL: {page_url}\nTITLE: {result.title}\n{result.markdown[:3000]}")
                        evidence.append({
                            "source": "firecrawl",
                            "url": page_url,
                            "title": result.title,
                            "summary": result.markdown[:500],
                            "evidence_category": "review_community",
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "status": "ok",
                        })
                    except Exception:
                        evidence.append({
                            "source": "firecrawl",
                            "url": page_url,
                            "summary": "Community page scrape failed",
                            "evidence_category": "review_community",
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "status": "failed",
                        })
            except Exception as exc:
                search_errors.append(f"firecrawl_audience_pages: {exc}")

        prompt_parts = [
            f"{self._business_strategy_system_prompt('Audience Research Analyst')}",
            f"Store URL: {store_url}",
            f"Operator notes: {description or 'None provided'}.",
            f"Store profile: {json.dumps(store_profile)}",
            f"Competitor context: {competitor_report[:3000]}",
        ]
        if search_results:
            prompt_parts.append(f"REVIEW AND COMMUNITY SEARCH RESULTS: {json.dumps(search_results, indent=2)}")
        if community_content:
            prompt_parts.append("COMMUNITY/REVIEW PAGE CONTENT:\n" + "\n---\n".join(community_content))
        if search_errors:
            prompt_parts.append(f"Search errors (partial data): {'; '.join(search_errors)}")
        prompt_parts.append(
            "Return ONLY valid JSON with keys: summary, findings, strategic_implications. "
            "findings must include target_audience, pain_points (list), objections (list), review_phrases (list), "
            "community_sources (list of source names or URLs), confidence_notes."
        )
        summary, model_used, estimated_cost = self._chat_complete(provider, model, "\n".join(prompt_parts), runtime_config)
        try:
            brief = json.loads(summary)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', summary)
            brief = json.loads(match.group(0)) if match else {
                "summary": f"Audience pain point research for {target}.",
                "findings": {"target_audience": target, "pain_points": [], "raw_response": summary},
                "strategic_implications": [],
            }
        return {
            "brief": brief,
            "model_used": model_used,
            "estimated_cost": estimated_cost,
            "evidence": evidence,
            "source_queries": source_queries,
            "search_errors": search_errors if search_errors else None,
        }

    def run_compliance_policy_research(
        self,
        store_url: str,
        description: str,
        store_profile: dict,
        competitor_report: str,
        *,
        provider: str,
        model: str,
        runtime_config: dict | None = None,
        tavily_api_key: str | None = None,
        firecrawl_api_key: str | None = None,
    ) -> dict:
        """Research platform policy and regulatory sources for claim/compliance risks."""
        from app.search import FirecrawlClient, TavilyClient

        search_errors: list[str] = []
        policy_results: list[dict] = []
        policy_pages: list[str] = []
        evidence: list[dict] = []
        source_queries: list[str] = []
        categories = store_profile.get("product_categories") or []
        cat_str = ", ".join(str(item) for item in categories[:3]) or description or store_url
        queries = [
            f"Meta advertising policies claims before after guarantee {cat_str}",
            f"TikTok Shop product listing policy restricted claims {cat_str}",
            f"FTC advertising substantiation endorsement review claims {cat_str}",
            f"Shopify prohibited items acceptable use policy {cat_str}",
        ]

        if tavily_api_key:
            try:
                tv = TavilyClient(api_key=tavily_api_key)
                for query in queries:
                    source_queries.append(query)
                    raw = tv.search_raw(query, max_results=4)
                    policy_results.append(raw)
                    for row in raw.get("results") or []:
                        url = row.get("url", "")
                        evidence.append({
                            "source": "tavily",
                            "url": url,
                            "title": row.get("title", ""),
                            "summary": row.get("content", ""),
                            "score": row.get("score"),
                            "query": query,
                            "evidence_category": "policy_regulatory",
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "status": "ok",
                        })
                        if url and any(domain in url.lower() for domain in ["facebook", "meta", "tiktok", "ftc.gov", "shopify"]):
                            policy_pages.append(url)
            except Exception as exc:
                search_errors.append(f"tavily_policy_search: {exc}")

        policy_content: list[str] = []
        if firecrawl_api_key and policy_pages:
            try:
                fc = FirecrawlClient(api_key=firecrawl_api_key)
                for page_url in policy_pages[:4]:
                    try:
                        result = fc.scrape(page_url)
                        policy_content.append(f"URL: {page_url}\nTITLE: {result.title}\n{result.markdown[:3000]}")
                        evidence.append({
                            "source": "firecrawl",
                            "url": page_url,
                            "title": result.title,
                            "summary": result.markdown[:500],
                            "evidence_category": "policy_regulatory",
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "status": "ok",
                        })
                    except Exception:
                        evidence.append({
                            "source": "firecrawl",
                            "url": page_url,
                            "summary": "Policy page scrape failed",
                            "evidence_category": "policy_regulatory",
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "status": "failed",
                        })
            except Exception as exc:
                search_errors.append(f"firecrawl_policy_pages: {exc}")

        prompt_parts = [
            f"{self._business_strategy_system_prompt('Compliance Research Analyst')}",
            f"Store URL: {store_url}",
            f"Operator notes: {description or 'None provided'}.",
            f"Store profile: {json.dumps(store_profile)}",
            f"Competitor context: {competitor_report[:3000]}",
        ]
        if policy_results:
            prompt_parts.append(f"POLICY AND REGULATORY SEARCH RESULTS: {json.dumps(policy_results, indent=2)}")
        if policy_content:
            prompt_parts.append("POLICY/REGULATORY PAGE CONTENT:\n" + "\n---\n".join(policy_content))
        if search_errors:
            prompt_parts.append(f"Search errors (partial data): {'; '.join(search_errors)}")
        prompt_parts.append(
            "Return ONLY valid JSON with keys: summary, findings, strategic_implications. "
            "findings must include policy_sources (list), flagged_terms (list), claims_to_verify (list), "
            "platform_risks (list), required_evidence (list), confidence_notes."
        )
        summary, model_used, estimated_cost = self._chat_complete(provider, model, "\n".join(prompt_parts), runtime_config)
        try:
            brief = json.loads(summary)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', summary)
            brief = json.loads(match.group(0)) if match else {
                "summary": f"Compliance policy research for {store_url}.",
                "findings": {"policy_sources": [], "flagged_terms": [], "raw_response": summary},
                "strategic_implications": [],
            }
        return {
            "brief": brief,
            "model_used": model_used,
            "estimated_cost": estimated_cost,
            "evidence": evidence,
            "source_queries": source_queries,
            "search_errors": search_errors if search_errors else None,
        }
