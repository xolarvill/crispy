import { useEffect, useState } from "react";
import { LayoutTemplate, RefreshCw } from "lucide-react";
import { api, type BrandCreativeSummary } from "./data";
import { Field } from "./components/console-controls";
import { Button } from "./components/ui/button";
import { promptTemplates } from "./prompt-templates";
import { pick, useI18n } from "./i18n";

export function PromptEditor({
  demo,
  prompt,
  onChange,
  workspace,
  onUseTemplate,
}: {
  demo: boolean;
  prompt: string;
  onChange: (value: string) => void;
  workspace?: string;
  onUseTemplate?: (templateId: string, workspace: string) => void;
}) {
  const { t, locale } = useI18n();
  const [idea, setIdea] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [hasResult, setHasResult] = useState(false);
  const [selection, setSelection] = useState(0);
  const [creatives, setCreatives] = useState<BrandCreativeSummary[]>([]);
  const [creativeId, setCreativeId] = useState("");
  const [creativeError, setCreativeError] = useState("");
  const [creativeRefresh, setCreativeRefresh] = useState(0);
  const selected = promptTemplates[selection];
  const selectedCreative = creatives.find((item) => item.id === creativeId);

  useEffect(() => {
    if (demo || !workspace) {
      setCreatives([]);
      setCreativeId("");
      setCreativeError("");
      return;
    }
    const controller = new AbortController();
    setCreativeError("");
    api<BrandCreativeSummary[]>(
      `/brand-creatives?workspace=${encodeURIComponent(workspace)}`,
      { signal: controller.signal },
    )
      .then((items) => {
        setCreatives(items);
        setCreativeId((current) =>
          items.some((item) => item.id === current) ? current : items[0]?.id || "",
        );
      })
      .catch((e) => {
        if (e.name !== "AbortError") setCreativeError(e.message);
      });
    return () => controller.abort();
  }, [demo, workspace, creativeRefresh]);

  useEffect(() => {
    const refresh = () => setCreativeRefresh((value) => value + 1);
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, []);

  async function expand() {
    setBusy(true);
    setStatus("");
    try {
      const result = await api<{ prompt: string }>("/image-prompts/expand", {
        method: "POST",
        body: JSON.stringify({ idea: idea.trim() }),
      });
      onChange(result.prompt);
      setHasResult(true);
    } catch (error) {
      setStatus((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(prompt);
      setStatus(t("Prompt copied", "已复制提示词"));
    } catch {
      setStatus(t("Select the full prompt and copy it manually", "请选中完整提示词，手动复制"));
    }
  }

  return (
    <details className="surface prompt-editor">
      <summary>{t("Image prompt expansion & template library", "画面提示词扩写与模板库")}</summary>
      <div className="form-group">
        <details className="prompt-library">
          <summary>{t("Browse prompt templates", "浏览提示词模板库")}</summary>
          <div className="prompt-library-body">
            <Field label={t("Choose a template", "选择模板")}>
              <select value={selection} onChange={(e) => setSelection(Number(e.target.value))}>
                {promptTemplates.map((template, index) => (
                  <option key={template.title.en} value={index}>{pick(template.title, locale)}</option>
                ))}
              </select>
            </Field>
            <p className="muted">{pick(selected.use, locale)}</p>
            <p className="prompt-preview">{pick(selected.prompt, locale)}</p>
            <Button variant="outline" disabled={busy} onClick={() => {
              onChange(pick(selected.prompt, locale));
              setHasResult(true);
              setStatus(t("Template loaded — edit the full prompt below", "模板已载入，可编辑下方的完整提示词"));
            }}>{prompt.trim() ? t("Replace the current prompt with this template", "用此模板替换当前提示词") : t("Use this template", "使用此模板")}</Button>
          </div>
        </details>
        <details className="prompt-library brand-library">
          <summary>{t("Brand templates · reuse a finished layout", "品牌模板库 · 从成品复用版式")}</summary>
          <div className="prompt-library-body">
            {demo ? (
              <p className="muted">{t("Demo mode does not read local brand templates.", "演示模式不读取本地品牌模板。")}</p>
            ) : !workspace ? (
              <p className="muted">{t("Enter the shop name in step one to read this shop's brand templates.", "先在第一步填写店铺名称，才能读取这家店的品牌模板。")}</p>
            ) : creativeError ? (
              <div className="preset-error" role="alert">
                {t("Could not load templates", "模板读取失败")}
                <Button variant="outline" onClick={() => setCreativeRefresh((value) => value + 1)}>
                  <RefreshCw size={14} />
                  {t("Retry", "重试")}
                </Button>
              </div>
            ) : creatives.length ? (
              <>
                <Field label={t("Choose a brand template", "选择品牌模板")}>
                  <select value={creativeId} onChange={(e) => setCreativeId(e.target.value)}>
                    {creatives.map((creative) => (
                      <option key={creative.id} value={creative.id}>
                        {creative.name} · {creative.ratio}
                      </option>
                    ))}
                  </select>
                </Field>
                {selectedCreative && (
                  <div className="brand-library-preview">
                    {selectedCreative.thumbnail ? (
                      <img src={selectedCreative.thumbnail} alt={t(`${selectedCreative.name} preview`, `${selectedCreative.name} 预览`)} />
                    ) : (
                      <span className="brand-library-empty">
                        <LayoutTemplate size={22} />
                      </span>
                    )}
                    <div>
                      <p>{selectedCreative.reason || t("No reason recorded yet", "还没有记录选用理由")}</p>
                      <small>
                        {t("Source task", "母版任务")} {selectedCreative.run_id.slice(0, 8)}
                        {selectedCreative.parent_id ? t(" · derived version", " · 派生版本") : ""}
                      </small>
                    </div>
                  </div>
                )}
                <div className="brand-library-actions">
                  <Button
                    disabled={!selectedCreative || !onUseTemplate}
                    onClick={() => {
                      if (selectedCreative && onUseTemplate)
                        onUseTemplate(selectedCreative.id, workspace);
                    }}
                  >
                    <LayoutTemplate size={14} />
                    {t("Use this template for a new product", "用此模板做新品")}
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => {
                      setCreativeRefresh((value) => value + 1);
                      setStatus(t("Brand templates refreshed", "已刷新品牌模板"));
                    }}
                  >
                    <RefreshCw size={14} />
                    {t("Refresh", "刷新")}
                  </Button>
                </div>
              </>
            ) : (
              <p className="muted">
                {t(
                  "This shop has no brand templates yet. Open a task's asset preview, pick an image you like, and choose “Save as brand template”.",
                  "这家店还没有品牌模板。打开任务素材预览，选一张满意的图片，点“存为品牌模板”。",
                )}
              </p>
            )}
          </div>
        </details>
        <Field label={t("Image idea", "画面想法")}>
          <textarea rows={2} maxLength={4000} value={idea} disabled={busy}
            placeholder={t("e.g. a noodle shop still lit on a rainy street in Chengdu, woodcut style", "例如：雨后的成都街头，一家还亮着灯的面馆，版画风格")}
            onChange={(e) => { setIdea(e.target.value); setStatus(""); }} />
        </Field>
        <Button variant="outline" disabled={demo || busy || !idea.trim()} onClick={expand}>
          {busy ? t("Expanding…", "正在扩写…") : t("Expand prompt", "扩写提示词")}
        </Button>
        {demo && <small>{t("Switch to the local API to expand prompts with a text model.", "切换到本地 API 模式后可调用文本模型扩写。")}</small>}
        {hasResult && <Field label={t("Full prompt", "完整提示词")}>
          <textarea rows={6} value={prompt} disabled={busy}
            onChange={(e) => { onChange(e.target.value); setStatus(""); }} />
        </Field>}
        {prompt && <Button variant="outline" onClick={copy}>{t("Copy prompt", "复制提示词")}</Button>}
        {status && <span role="status">{status}</span>}
      </div>
    </details>
  );
}
