export type Run = {
  id: string;
  product_code: string;
  status: string;
  current_stage: string | null;
  pipeline_mode: string;
  updated_at: string;
  project_id: string;
  name?: string;
  image?: string;
  stage_tasks?: {
    id: string;
    stage_name: string;
    status: string;
    error_message?: string | null;
  }[];
};
export const stageNames: Record<string, string> = {
  intake: "需求确认",
  planning: "策略规划",
  divergence: "创意方向",
  copy_image_generation: "图文生成",
  video_scripting: "视频脚本",
  storyboard_image_generation: "分镜生成",
  video_generation: "视频生成",
  visual_quality_assessment: "视觉质检",
  evaluation_selection: "评估交付",
};
export const statusNames: Record<string, string> = {
  waiting_review: "待审核",
  running: "进行中",
  queued: "排队中",
  draft: "草稿",
  not_started: "未开始",
  approved: "已通过",
  completed: "已完成",
  failed: "失败",
  rejected: "已驳回",
};
export const modes: Record<string, string> = {
  copy_image_only: "图文",
  video_only: "视频",
  full_multimodal: "图文 + 视频",
  marketplace_main_image: "电商主图",
  tiktok_shop_video: "TikTok 视频",
};
export function plan(run: Run) {
  const stages = ["intake", "planning", "divergence"];
  if (!["video_only", "tiktok_shop_video"].includes(run.pipeline_mode))
    stages.push("copy_image_generation");
  if (
    ["video_only", "tiktok_shop_video", "full_multimodal"].includes(
      run.pipeline_mode,
    )
  )
    stages.push(
      "video_scripting",
      "storyboard_image_generation",
      "video_generation",
    );
  return [...stages, "visual_quality_assessment", "evaluation_selection"];
}
export function progress(run: Run) {
  const groups = [
    ["intake"],
    ["planning", "divergence"],
    [
      "copy_image_generation",
      "video_scripting",
      "storyboard_image_generation",
      "video_generation",
    ],
    ["visual_quality_assessment"],
    ["evaluation_selection"],
  ];
  const active = groups.findIndex((g) => g.includes(run.current_stage || ""));
  return ["需求", "策略", "生成", "质检", "交付"].map((label, i) => ({
    label,
    state:
      run.status === "completed" || (active >= 0 && i < active)
        ? "done"
        : i === active
          ? "now"
          : "",
  }));
}
export const demoRuns: Run[] = [
  {
    id: "demo-0832",
    name: "夏日亚麻连衣裙",
    product_code: "LINEN-032",
    status: "waiting_review",
    current_stage: "copy_image_generation",
    pipeline_mode: "copy_image_only",
    updated_at: "2026-10-31T06:28:00Z",
    project_id: "demo",
    image: "/dress.jpg",
  },
  {
    id: "demo-0831",
    name: "银色缎面长裙",
    product_code: "SATIN-031",
    status: "running",
    current_stage: "visual_quality_assessment",
    pipeline_mode: "marketplace_main_image",
    updated_at: "2026-10-31T03:03:00Z",
    project_id: "demo",
    image: "/silver.jpg",
  },
  {
    id: "demo-0830",
    name: "居家棉质长袍",
    product_code: "ROBE-030",
    status: "completed",
    current_stage: "evaluation_selection",
    pipeline_mode: "full_multimodal",
    updated_at: "2026-10-30T08:20:00Z",
    project_id: "demo",
    image: "/robe.jpg",
  },
  {
    id: "demo-0829",
    name: "秋季系列 · 短视频",
    product_code: "AUTUMN-029",
    status: "queued",
    current_stage: "video_scripting",
    pipeline_mode: "video_only",
    updated_at: "2026-10-30T06:11:00Z",
    project_id: "demo",
    image: "/dress.jpg",
  },
  {
    id: "demo-0828",
    name: "缎面系列 · 主图",
    product_code: "SATIN-028",
    status: "waiting_review",
    current_stage: "planning",
    pipeline_mode: "marketplace_main_image",
    updated_at: "2026-10-30T02:05:00Z",
    project_id: "demo",
    image: "/silver.jpg",
  },
  {
    id: "demo-0827",
    name: "冬日居家系列",
    product_code: "HOME-027",
    status: "failed",
    current_stage: "copy_image_generation",
    pipeline_mode: "copy_image_only",
    updated_at: "2026-10-29T10:32:00Z",
    project_id: "demo",
    image: "/robe.jpg",
  },
];
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch("/api" + path, {
    ...options,
    headers: {
      ...(options?.body instanceof FormData
        ? {}
        : { "Content-Type": "application/json" }),
      ...options?.headers,
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : `请求失败 (${res.status})`,
    );
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}
