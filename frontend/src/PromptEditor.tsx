import { useState } from "react";
import { api } from "./data";
import { Field } from "./components/console-controls";
import { Button } from "./components/ui/button";

export function PromptEditor({ demo, prompt, onChange }: {
  demo: boolean;
  prompt: string;
  onChange: (value: string) => void;
}) {
  const [idea, setIdea] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [hasResult, setHasResult] = useState(false);

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
      setStatus("已复制提示词");
    } catch {
      setStatus("请选中完整提示词，手动复制");
    }
  }

  return (
    <details className="surface prompt-editor">
      <summary>画面提示词扩写</summary>
      <div className="form-group">
        <Field label="画面想法">
          <textarea rows={2} maxLength={4000} value={idea} disabled={busy}
            placeholder="例如：雨后的成都街头，一家还亮着灯的面馆，版画风格"
            onChange={(e) => { setIdea(e.target.value); setStatus(""); }} />
        </Field>
        <Button variant="outline" disabled={demo || busy || !idea.trim()} onClick={expand}>
          {busy ? "正在扩写…" : "扩写提示词"}
        </Button>
        {demo && <small>切换到本地 API 模式后可调用文本模型扩写。</small>}
        {hasResult && <Field label="完整提示词">
          <textarea rows={6} value={prompt} disabled={busy}
            onChange={(e) => { onChange(e.target.value); setStatus(""); }} />
        </Field>}
        {prompt && <Button variant="outline" onClick={copy}>复制提示词</Button>}
        {status && <span role="status">{status}</span>}
      </div>
    </details>
  );
}
