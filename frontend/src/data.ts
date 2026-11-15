import { currentLocale, pick, type Localized, type Locale } from "./i18n";

export type RunMedia = {
  uri: string;
  kind: "image" | "video";
  source: "generated" | "input";
};
export type BrandCreativeLayout = {
  ratio: "1:1" | "3:4" | "16:9";
  title: string;
  titleX: number;
  titleY: number;
  color: "#ffffff" | "#171717";
  cropX: number;
  cropY: number;
  logo: string;
  logoX: number;
  logoY: number;
};
export type BrandCreativeRegion = {
  x: number;
  y: number;
  width: number;
  height: number;
  note: string;
};
export type BrandCreativeSummary = {
  id: string;
  name: string;
  parent_id: string | null;
  run_id: string;
  reason: string;
  ratio: BrandCreativeLayout["ratio"];
  thumbnail: string;
  created_at: string | null;
};
export type BrandCreative = BrandCreativeSummary & {
  uri: string;
  prompt: string;
  image: string;
  base_image?: string;
  layout: BrandCreativeLayout;
  regions: BrandCreativeRegion[];
};
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
  media_count?: number;
  media_previews?: RunMedia[];
  stage_tasks?: {
    id: string;
    stage_name: string;
    status: string;
    error_message?: string | null;
  }[];
};
export const stageNames: Record<string, Localized> = {
  intake: { en: "Intake", zh: "需求确认" },
  planning: { en: "Planning", zh: "策略规划" },
  divergence: { en: "Creative direction", zh: "创意方向" },
  copy_image_generation: { en: "Copy & image", zh: "图文生成" },
  video_scripting: { en: "Video script", zh: "视频脚本" },
  storyboard_image_generation: { en: "Storyboard", zh: "分镜生成" },
  video_generation: { en: "Video", zh: "视频生成" },
  visual_quality_assessment: { en: "Visual QA", zh: "视觉质检" },
  evaluation_selection: { en: "Evaluation & delivery", zh: "评估交付" },
};
export const statusNames: Record<string, Localized> = {
  waiting_review: { en: "Needs review", zh: "待审核" },
  running: { en: "Running", zh: "进行中" },
  queued: { en: "Queued", zh: "排队中" },
  draft: { en: "Draft", zh: "草稿" },
  not_started: { en: "Not started", zh: "未开始" },
  approved: { en: "Approved", zh: "已通过" },
  completed: { en: "Completed", zh: "已完成" },
  failed: { en: "Failed", zh: "失败" },
  rejected: { en: "Rejected", zh: "已驳回" },
};
export const modes: Record<string, Localized> = {
  copy_image_only: { en: "Copy & image", zh: "图文" },
  video_only: { en: "Video", zh: "视频" },
  full_multimodal: { en: "Copy, image & video", zh: "图文 + 视频" },
  marketplace_main_image: { en: "Marketplace hero", zh: "电商主图" },
  tiktok_shop_video: { en: "TikTok video", zh: "TikTok 视频" },
};

function localizedLabel(
  labels: Record<string, Localized>,
  key: string | null | undefined,
  locale: Locale,
): string {
  const value = key ? labels[key] : undefined;
  return value ? pick(value, locale) : "";
}
export function stageLabel(key: string | null | undefined, locale: Locale) {
  return localizedLabel(stageNames, key, locale);
}
export function statusLabel(key: string | null | undefined, locale: Locale) {
  return localizedLabel(statusNames, key, locale);
}
export function modeLabel(key: string | null | undefined, locale: Locale) {
  return localizedLabel(modes, key, locale);
}
export type StageItem = {
  id: string;
  stage_name: string;
  status: string;
  error_message?: string | null;
};
export function stageItems(run: Run, demo: boolean): StageItem[] {
  const stages = plan(run),
    order = new Map(stages.map((name, i) => [name, i]));
  if (!demo) {
    return [...(run.stage_tasks ?? [])].sort(
      (a, b) =>
        (order.get(a.stage_name) ?? stages.length) -
        (order.get(b.stage_name) ?? stages.length),
    );
  }
  const active = stages.indexOf(run.current_stage || "");
  return stages.map((stage_name, i) => ({
    id: stage_name,
    stage_name,
    status:
      run.status === "completed" || (active >= 0 && i < active)
        ? "completed"
        : i === active
          ? run.status
          : "not_started",
    error_message: null,
  }));
}
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
export function progress(run: Run, locale: Locale) {
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
  const labels: Localized[] = [
    { en: "Brief", zh: "需求" },
    { en: "Strategy", zh: "策略" },
    { en: "Generate", zh: "生成" },
    { en: "QA", zh: "质检" },
    { en: "Deliver", zh: "交付" },
  ];
  return labels.map((label, i) => ({
    label: pick(label, locale),
    state:
      run.status === "completed" || (active >= 0 && i < active)
        ? "done"
        : i === active
          ? "now"
          : "",
  }));
}
export function demoRuns(locale: Locale): Run[] {
  const name = (en: string, zh: string) => pick({ en, zh }, locale);
  return [
  {
    id: "demo-0832",
    name: name("Summer linen dress", "夏日亚麻连衣裙"),
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
    name: name("Silver satin gown", "银色缎面长裙"),
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
    name: name("Spring/summer reference set", "春夏服饰 · 参考素材"),
    product_code: "STYLE-030",
    status: "completed",
    current_stage: "evaluation_selection",
    pipeline_mode: "full_multimodal",
    updated_at: "2026-10-30T08:20:00Z",
    project_id: "demo",
    image: "/robe.jpg",
    media_count: 3,
    media_previews: [
      { uri: "/robe.jpg", kind: "image", source: "input" },
      { uri: "/dress.jpg", kind: "image", source: "input" },
      { uri: "/silver.jpg", kind: "image", source: "input" },
    ],
  },
  {
    id: "demo-0829",
    name: name("Autumn series · short video", "秋季系列 · 短视频"),
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
    name: name("Satin series · hero image", "缎面系列 · 主图"),
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
    name: name("Winter home collection", "冬日居家系列"),
    product_code: "HOME-027",
    status: "failed",
    current_stage: "copy_image_generation",
    pipeline_mode: "copy_image_only",
    updated_at: "2026-10-29T10:32:00Z",
    project_id: "demo",
    image: "/robe.jpg",
  },
  ];
}
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
    const error = new Error(
      typeof body?.detail === "string"
        ? body.detail
        : currentLocale() === "zh"
          ? `请求失败 (${res.status})`
          : `Request failed (${res.status})`,
    ) as Error & { status?: number };
    error.status = res.status;
    throw error;
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}
