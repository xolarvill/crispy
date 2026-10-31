import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Layers,
  LoaderCircle,
  Plus,
  Upload,
  X,
} from "lucide-react";
import { Button } from "./components/ui/button";
import { Field, IconButton } from "./components/console-controls";
import { go } from "./navigation";
import { api, modes, type Run } from "./data";
type Draft = {
  name: string;
  code: string;
  industry: string;
  workspace: string;
  project: string;
  campaign: string;
  mode: string;
  approval: string;
  preset: string;
  count: string;
};
const initial: Draft = {
  name: "",
  code: "",
  industry: "",
  workspace: "",
  project: "",
  campaign: "",
  mode: "copy_image_only",
  approval: "manual",
  preset: "",
  count: "3",
};
const approvals: Record<string, string> = {
  manual: "逐阶段审核",
  semi_auto: "创意阶段审核",
  full_auto: "自动执行",
};
type Preset = { value: string; label: string; spec: Record<string, unknown> };
const demoPresets: Preset[] = [
  {
    value: "demo_square",
    label: "演示 · 方形图文",
    spec: {
      image_size: "1:1",
      video_size: "1:1",
      resolution: "720p",
      video_duration_seconds: 5,
    },
  },
  {
    value: "demo_vertical",
    label: "演示 · 竖屏视频",
    spec: {
      image_size: "9:16",
      video_size: "9:16",
      resolution: "720p",
      video_duration_seconds: 5,
    },
  },
];
export default function CreatePage({
  demo,
  onCreated,
}: {
  demo: boolean;
  onCreated: (run: Run) => void;
}) {
  const [step, setStep] = useState(0),
    [draft, setDraft] = useState<Draft>(initial),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [files, setFiles] = useState<File[]>([]),
    [presets, setPresets] = useState<Preset[]>(demo ? demoPresets : []),
    [presetError, setPresetError] = useState(""),
    [presetRefresh, setPresetRefresh] = useState(0),
    [warnings, setWarnings] = useState<string[]>([]);
  useEffect(() => {
    if (demo) {
      setPresets(demoPresets);
      return;
    }
    const controller = new AbortController();
    setPresets([]);
    setPresetError("");
    api<{
      system: ({ key: string; name?: string } & Record<string, unknown>)[];
      user: ({ id: string; name: string } & Record<string, unknown>)[];
    }>(
      `/creative-presets?workspace_name=${encodeURIComponent(draft.workspace.trim() || "workspace_demo")}`,
      { signal: controller.signal },
    )
      .then((data) => {
        const next = [
          ...data.system.map((p) => ({
            value: p.key,
            label: p.name || p.key,
            spec: p,
          })),
          ...data.user.map((p) => ({
            value: "user_" + p.id,
            label: p.name,
            spec: p,
          })),
        ];
        setPresets(next);
        setDraft((d) => ({
          ...d,
          preset: next.some((p) => p.value === d.preset) ? d.preset : "",
        }));
      })
      .catch((e) => {
        if (e.name !== "AbortError") setPresetError(e.message);
      });
    return () => controller.abort();
  }, [demo, draft.workspace, presetRefresh]);
  function selectFiles(selected: FileList | null) {
    if (!selected) return;
    const next = [...files, ...Array.from(selected)];
    const allowed = /\.(csv|xlsx|png|jpe?g|webp|mp4|mov|m4v)$/i;
    if (
      next.length > 10 ||
      next.some((f) => f.size > 50 * 1024 * 1024) ||
      next.reduce((sum, f) => sum + f.size, 0) > 200 * 1024 * 1024
    ) {
      setError("最多 10 个文件，单个 50 MB，总计 200 MB");
      return;
    }
    if (next.some((f) => !allowed.test(f.name))) {
      setError("支持 CSV、XLSX、图片和视频");
      return;
    }
    setFiles(next);
    setError("");
  }
  function change(key: keyof Draft, value: string) {
    setDraft((d) => ({ ...d, [key]: value }));
    setWarnings([]);
  }
  function input(key: keyof Draft, label: string, placeholder: string) {
    return (
      <Field label={label}>
        <input
          name={key}
          value={draft[key]}
          required
          maxLength={120}
          placeholder={placeholder}
          onChange={(e) => change(key, e.target.value)}
        />
      </Field>
    );
  }
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError("");
    if (step < 2) {
      const required =
        step === 0
          ? ([
              "name",
              "code",
              "industry",
              "workspace",
              "project",
              "campaign",
            ] as const)
          : (["preset"] as const);
      if (required.some((key) => !draft[key].trim())) {
        setError("请填写必填项");
        return;
      }
      setStep((s) => s + 1);
      return;
    }
    setBusy(true);
    const payload = {
      workspace_name: draft.workspace.trim(),
      project_name: draft.project.trim(),
      product_name: draft.name.trim(),
      product_code: draft.code.trim(),
      industry_code: draft.industry.trim(),
      campaign_name: draft.campaign.trim(),
      creative_preset: draft.preset.trim(),
      creative_specs: presets.find((p) => p.value === draft.preset)?.spec || {},
      pipeline_mode: draft.mode,
      approval_mode: draft.approval,
      variant_count: Number(draft.count),
      market: "US",
      locale: "en-US",
    };
    try {
      let run: Run;
      if (demo) {
        run = {
          id: `demo-${Date.now()}`,
          name: payload.product_name,
          product_code: payload.product_code,
          status: "draft",
          current_stage: "intake",
          pipeline_mode: draft.mode,
          updated_at: new Date().toISOString(),
          project_id: "demo",
        };
      } else {
        const preflight = await api<{
          severity: string;
          checks: { message: string; severity: string }[];
        }>("/runs/preflight", {
          method: "POST",
          body: JSON.stringify({
            pipeline_mode: draft.mode,
            has_image_inputs: files.some((f) =>
              /\.(png|jpe?g|webp)$/i.test(f.name),
            ),
            has_video_inputs: files.some((f) =>
              /\.(mp4|mov|m4v)$/i.test(f.name),
            ),
            creative_specs: payload.creative_specs,
          }),
        });
        if (preflight.severity === "error")
          throw Error(preflight.checks.map((c) => c.message).join("；"));
        const nextWarnings = preflight.checks
          .filter((c) => c.severity === "warn")
          .map((c) => c.message);
        if (
          nextWarnings.length &&
          JSON.stringify(nextWarnings) !== JSON.stringify(warnings)
        ) {
          setWarnings(nextWarnings);
          return;
        }
        const body = new FormData();
        for (const [key, value] of Object.entries(payload))
          body.append(
            key,
            typeof value === "object" ? JSON.stringify(value) : String(value),
          );
        for (const file of files) body.append("files", file);
        run = await api<Run>("/runs/rich", {
          method: "POST",
          body,
        });
      }
      onCreated(run);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const summary = [
    ["产品", draft.name || "—"],
    ["编号", draft.code || "—"],
    ["店铺", draft.workspace || "—"],
    ["项目", draft.project || "—"],
    ["活动", draft.campaign || "—"],
    ["行业", draft.industry || "—"],
    ["创作模式", modes[draft.mode]],
    ["审核方式", approvals[draft.approval]],
    ["创意预设", draft.preset || "—"],
    ["变体数量", draft.count],
    ["市场 / 语言", "US / en-US"],
    ["参考文件", `${files.length} 个`],
  ];
  return (
    <>
      <div className="resource-header">
        <div className="resource-name">
          <IconButton
            icon={ArrowLeft}
            label="返回任务列表"
            onClick={() => go("/tasks")}
          />
          <p className="page-title">新建任务</p>
        </div>
        <span className="muted">{demo ? "演示 · 不执行生成" : "本地 API"}</span>
      </div>
      <ol className="creation-steps" aria-label="创建步骤">
        {["产品与项目", "创作设置", "确认创建"].map((label, i) => (
          <li
            key={label}
            aria-current={step === i ? "step" : undefined}
            className={step === i ? "active" : step > i ? "done" : ""}
          >
            <span>{step > i ? <Check size={13} /> : i + 1}</span>
            {label}
          </li>
        ))}
      </ol>
      <form
        onSubmit={submit}
        className={`creation-layout ${step === 2 ? "confirmation" : ""}`}
      >
        <section className="surface creation-form">
          <div className="form-heading">
            <p>{["产品与项目", "创作设置", "确认任务"][step]}</p>
            <span className="muted">{step + 1} / 3</span>
          </div>
          <div className="form-body">
            {step === 0 ? (
              <>
                <div className="form-group">
                  <div className="form-row">
                    {input("name", "产品名称", "产品名称")}
                    {input("code", "产品编号", "SKU / 产品编号")}
                  </div>
                  {input("industry", "行业", "例如 apparel")}
                </div>
                <div className="form-group">
                  <div className="form-row">
                    {input("workspace", "店铺", "店铺名称")}
                    {input("project", "项目", "项目名称")}
                  </div>
                  {input("campaign", "推广活动", "活动名称")}
                  <label className="upload-control">
                    <span>
                      <Upload size={16} />
                      添加参考素材
                    </span>
                    <small className="muted">图片 / 视频 / 表格</small>
                    <input
                      type="file"
                      multiple
                      accept=".csv,.xlsx,.png,.jpg,.jpeg,.webp,.mp4,.mov,.m4v"
                      aria-label="添加参考素材"
                      onChange={(e) => {
                        selectFiles(e.target.files);
                        e.target.value = "";
                      }}
                    />
                  </label>
                  {!!files.length && (
                    <ul className="file-list">
                      {files.map((file, i) => (
                        <li key={`${file.name}-${i}`}>
                          <span>{file.name}</span>
                          <IconButton
                            icon={X}
                            label={`移除 ${file.name}`}
                            onClick={() => {
                              setFiles((list) =>
                                list.filter((_, index) => index !== i),
                              );
                              setWarnings([]);
                            }}
                          />
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </>
            ) : step === 1 ? (
              <>
                <div className="form-group">
                  <Field label="创作模式">
                    <select
                      value={draft.mode}
                      onChange={(e) => change("mode", e.target.value)}
                    >
                      {Object.entries(modes).map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="审核方式">
                    <select
                      value={draft.approval}
                      onChange={(e) => change("approval", e.target.value)}
                    >
                      {Object.entries(approvals).map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
                <div className="form-group">
                  <div className="form-row">
                    <Field label="创意预设">
                      <select
                        required
                        value={draft.preset}
                        onChange={(e) => change("preset", e.target.value)}
                      >
                        <option value="">选择预设</option>
                        {presets.map((p) => (
                          <option key={p.value} value={p.value}>
                            {p.label}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Field label="变体数量">
                      <input
                        type="number"
                        required
                        min="1"
                        max="20"
                        value={draft.count}
                        onChange={(e) => change("count", e.target.value)}
                      />
                    </Field>
                  </div>
                  <dl className="fixed-settings">
                    <div>
                      <dt>市场</dt>
                      <dd>US</dd>
                    </div>
                    <div>
                      <dt>语言</dt>
                      <dd>en-US</dd>
                    </div>
                  </dl>
                  {presetError && (
                    <div className="preset-error" role="alert">
                      预设加载失败
                      <Button
                        variant="outline"
                        onClick={() => setPresetRefresh((r) => r + 1)}
                      >
                        重试
                      </Button>
                    </div>
                  )}
                </div>
              </>
            ) : (
              <dl className="confirmation-list">
                {summary.map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            )}
            {error && (
              <div className="alert" role="alert">
                {error}
              </div>
            )}
            {!!warnings.length && (
              <div className="preflight-warning" role="alert">
                {warnings.map((w) => (
                  <p key={w}>{w}</p>
                ))}
              </div>
            )}
          </div>
          <div className="form-actions">
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                step === 0 ? go("/tasks") : setStep((s) => s - 1)
              }
            >
              {step === 0 ? (
                "取消"
              ) : (
                <>
                  <ArrowLeft size={14} />
                  上一步
                </>
              )}
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? (
                <LoaderCircle className="spin" size={15} />
              ) : step === 2 ? (
                <Plus size={15} />
              ) : null}
              {step === 2
                ? warnings.length
                  ? "仍然创建"
                  : "创建任务"
                : "下一步"}
              {step < 2 && <ArrowRight size={14} />}
            </Button>
          </div>
        </section>
        {step < 2 && (
          <aside className="creation-summary" aria-label="任务摘要">
            <div className="summary-symbol">
              <Layers size={22} />
            </div>
            <p>任务摘要</p>
            <dl>
              {summary.slice(0, step === 0 ? 4 : 10).map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          </aside>
        )}
      </form>
    </>
  );
}
