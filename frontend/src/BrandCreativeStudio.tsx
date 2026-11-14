import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Check, Download, Image as ImageIcon, LoaderCircle, Trash2, Upload, X } from "lucide-react";
import type Konva from "konva";
import { Image as KonvaImage, Layer, Rect, Stage, Text as KonvaText } from "react-konva";
import { Button } from "./components/ui/button";
import { Field, IconButton } from "./components/console-controls";
import {
  api,
  type BrandCreative,
  type BrandCreativeLayout,
  type BrandCreativeRegion,
} from "./data";

type Ratio = BrandCreativeLayout["ratio"];
const ratios: { value: Ratio; label: string; valueNumber: number }[] = [
  { value: "1:1", label: "方形 1:1", valueNumber: 1 },
  { value: "3:4", label: "竖版 3:4", valueNumber: 3 / 4 },
  { value: "16:9", label: "横版 16:9", valueNumber: 16 / 9 },
];

const emptyLayout: BrandCreativeLayout = {
  ratio: "3:4",
  title: "",
  titleX: 0.08,
  titleY: 0.12,
  color: "#ffffff",
  cropX: 0.5,
  cropY: 0.5,
  logo: "",
  logoX: 0.78,
  logoY: 0.06,
};

function clamp(value: number, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value));
}

function mediaUrl(uri: string) {
  if (/^(data:|https?:\/\/)/i.test(uri)) return uri;
  return `/api/media?path=${encodeURIComponent(uri)}`;
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    if (/^https?:\/\//i.test(src)) image.crossOrigin = "anonymous";
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("图片加载失败"));
    image.src = src;
  });
}

async function toBaseDataUrl(src: string): Promise<string> {
  const image = await loadImage(src);
  const max = 1600;
  const scale = Math.min(1, max / Math.max(image.naturalWidth, image.naturalHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("无法创建画布");
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.9);
}

function cropFor(image: HTMLImageElement, aspect: number, cropX: number, cropY: number) {
  const width = image.naturalWidth,
    height = image.naturalHeight,
    imageAspect = width / height;
  let cropWidth: number, cropHeight: number;
  if (imageAspect > aspect) {
    cropHeight = height;
    cropWidth = height * aspect;
  } else {
    cropWidth = width;
    cropHeight = width / aspect;
  }
  return {
    x: (width - cropWidth) * clamp(cropX),
    y: (height - cropHeight) * clamp(cropY),
    width: cropWidth,
    height: cropHeight,
  };
}

async function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.readAsDataURL(file);
  });
}

async function logoDataUrl(file: File): Promise<string> {
  const raw = await readFileAsDataUrl(file);
  const image = await loadImage(raw);
  const max = 320;
  const scale = Math.min(1, max / Math.max(image.naturalWidth, image.naturalHeight));
  if (scale === 1) return raw;
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext("2d");
  if (!context) return raw;
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/png");
}

export type StudioTarget =
  | { kind: "asset"; runId: string; uri: string; workspace?: string }
  | { kind: "template"; templateId: string; workspace: string };

export function BrandCreativeStudio({
  target,
  close,
  notify,
}: {
  target: StudioTarget;
  close: () => void;
  notify?: (message: string) => void;
}) {
  const [original, setOriginal] = useState<BrandCreative | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [prompt, setPrompt] = useState("");
  const [layout, setLayout] = useState<BrandCreativeLayout>(emptyLayout);
  const [regions, setRegions] = useState<BrandCreativeRegion[]>([]);
  const [baseSrc, setBaseSrc] = useState("");
  const [baseImage, setBaseImage] = useState<HTMLImageElement | null>(null);
  const [logoImage, setLogoImage] = useState<HTMLImageElement | null>(null);
  const [replaced, setReplaced] = useState(false);
  const [mode, setMode] = useState<"layout" | "annotate">("layout");
  const [draftRegion, setDraftRegion] = useState<BrandCreativeRegion | null>(null);
  const [pendingRegion, setPendingRegion] = useState<BrandCreativeRegion | null>(null);
  const [note, setNote] = useState("");
  const drawing = useRef<{ x: number; y: number } | null>(null);
  const stageRef = useRef<Konva.Stage>(null);
  const regionLayerRef = useRef<Konva.Layer>(null);
  const containerObserver = useRef<ResizeObserver | null>(null);
  const [containerSize, setContainerSize] = useState({ width: 760, height: 780 });

  const ratioValue = ratios.find((item) => item.value === layout.ratio)?.valueNumber ?? 3 / 4;
  const stageWidth = Math.max(
    240,
    Math.min(
      Math.max(280, (containerSize.width || 760) - 40),
      Math.max(260, (containerSize.height || 780) - 120) * ratioValue,
      760,
    ),
  );
  const stageHeight = Math.round(stageWidth / ratioValue);
  const saveRunId = original?.run_id || (target.kind === "asset" ? target.runId : "");

  const attachContainer = useCallback((node: HTMLDivElement | null) => {
    containerObserver.current?.disconnect();
    containerObserver.current = null;
    if (!node) return;
    const update = () =>
      setContainerSize({ width: node.clientWidth, height: node.clientHeight });
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    containerObserver.current = observer;
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    if (target.kind === "template") {
      api<BrandCreative>(
        `/brand-creatives/${encodeURIComponent(target.templateId)}?workspace=${encodeURIComponent(target.workspace)}`,
        { signal: controller.signal },
      )
        .then((creative) => {
          setOriginal(creative);
          setName(`${creative.name} · 新版`);
          setReason(creative.reason || "");
          setPrompt(creative.prompt || "");
          const legacy = !creative.base_image;
          setLayout({
            ...emptyLayout,
            ...creative.layout,
            ...(legacy ? { title: "", logo: "" } : {}),
          });
          setRegions(creative.regions || []);
          setBaseSrc(creative.base_image || creative.image);
          setReplaced(false);
          if (legacy)
            setStatus("母版只保留了成品图，标题或 Logo 可能已经叠在底图上；需要重新叠字时请换一张底图。");
        })
        .catch((e) => {
          if (e.name !== "AbortError") setError((e as Error).message);
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
      return () => controller.abort();
    }
    api<{ prompt: string; workspace: string }>(
      `/runs/${encodeURIComponent(target.runId)}/creative-source?uri=${encodeURIComponent(target.uri)}`,
      { signal: controller.signal },
    )
      .then(async (source) => {
        setPrompt(source.prompt || "");
        try {
          const dataUrl = await toBaseDataUrl(mediaUrl(target.uri));
          if (!controller.signal.aborted) setBaseSrc(dataUrl);
        } catch (e) {
          if (!controller.signal.aborted) {
            setBaseSrc(mediaUrl(target.uri));
            setError(
              `${(e as Error).message}。这张图片可能来自跨域地址，可继续预览，但无法导出或保存模板。`,
            );
          }
        }
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError((e as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [target]);

  useEffect(() => {
    if (!baseSrc) return;
    let live = true;
    setBaseImage(null);
    loadImage(baseSrc)
      .then((image) => {
        if (live) setBaseImage(image);
      })
      .catch((e) => {
        if (live) setError((e as Error).message);
      });
    return () => {
      live = false;
    };
  }, [baseSrc]);

  useEffect(() => {
    if (!layout.logo) {
      setLogoImage(null);
      return;
    }
    let live = true;
    loadImage(layout.logo)
      .then((image) => {
        if (live) setLogoImage(image);
      })
      .catch(() => {
        if (live) setLogoImage(null);
      });
    return () => {
      live = false;
    };
  }, [layout.logo]);

  const crop = useMemo(
    () => (baseImage ? cropFor(baseImage, ratioValue, layout.cropX, layout.cropY) : null),
    [baseImage, ratioValue, layout.cropX, layout.cropY],
  );
  const titleSize = Math.max(18, Math.round(stageWidth * (layout.ratio === "16:9" ? 0.05 : 0.062)));
  const logoWidth = Math.round(stageWidth * 0.16);
  const logoHeight = logoImage ? Math.round(logoWidth * (logoImage.naturalHeight / logoImage.naturalWidth)) : 0;

  function pointer() {
    const position = stageRef.current?.getPointerPosition();
    const stage = stageRef.current;
    if (!position || !stage) return null;
    return { x: clamp(position.x / stage.width()), y: clamp(position.y / stage.height()) };
  }

  function startRegion() {
    if (mode !== "annotate") return;
    const point = pointer();
    if (!point) return;
    drawing.current = point;
    setPendingRegion(null);
    setDraftRegion({ ...point, width: 0, height: 0, note: "" });
  }

  function moveRegion() {
    if (mode !== "annotate" || !drawing.current) return;
    const point = pointer();
    const start = drawing.current;
    if (!point) return;
    setDraftRegion({
      x: Math.min(start.x, point.x),
      y: Math.min(start.y, point.y),
      width: Math.abs(point.x - start.x),
      height: Math.abs(point.y - start.y),
      note: "",
    });
  }

  function finishRegion() {
    if (mode !== "annotate" || !drawing.current) return;
    drawing.current = null;
    const region = draftRegion;
    setDraftRegion(null);
    if (!region || region.width < 0.015 || region.height < 0.015) return;
    setPendingRegion(region);
    setNote("");
    setStatus("给刚框出的区域写一句修改说明，再点“添加批注”。");
  }

  function addRegion() {
    if (!pendingRegion || !note.trim()) return;
    setRegions((items) => [...items, { ...pendingRegion, note: note.trim() }]);
    setPendingRegion(null);
    setNote("");
    setStatus("批注已加入，可继续框选。");
  }

  async function pickReplacement(file: File | undefined) {
    if (!file) return;
    if (!/^image\/(png|jpe?g|webp)$/.test(file.type)) {
      setStatus("请选择 PNG、JPG 或 WebP 图片");
      return;
    }
    const dataUrl = await toBaseDataUrl(await readFileAsDataUrl(file)).catch((e) => {
      setStatus((e as Error).message);
      return "";
    });
    if (!dataUrl) return;
    setBaseSrc(dataUrl);
    setReplaced(true);
    setStatus("已换成新底图，裁切和标题位置保持不变。");
  }

  async function pickLogo(file: File | undefined) {
    if (!file) return;
    if (!/^image\/(png|jpe?g|webp)$/.test(file.type)) {
      setStatus("Logo 需要 PNG、JPG 或 WebP");
      return;
    }
    const dataUrl = await logoDataUrl(file).catch((e) => {
      setStatus((e as Error).message);
      return "";
    });
    if (dataUrl) setLayout((value) => ({ ...value, logo: dataUrl }));
  }

  function capture(mimeType: "image/png" | "image/jpeg", quality?: number) {
    const stage = stageRef.current,
      regionsLayer = regionLayerRef.current;
    if (!stage) return "";
    // Annotations are editing notes, not part of the exported creative.
    const visible = regionsLayer?.visible() ?? false;
    regionsLayer?.visible(false);
    stage.draw();
    const image = stage.toDataURL({ pixelRatio: 2, mimeType, quality });
    regionsLayer?.visible(visible);
    stage.draw();
    return image;
  }

  async function snapshot() {
    const image = capture("image/jpeg", 0.9);
    if (!image) throw new Error("画布尚未就绪");
    return image;
  }

  function download(ratio: Ratio) {
    setLayout((value) => ({ ...value, ratio }));
    window.setTimeout(() => {
      try {
        const image = capture("image/png");
        if (!image) return;
        const link = document.createElement("a");
        link.href = image;
        link.download = `${(name || "brand-creative").replace(/[\\/:*?"<>|]/g, "-")}-${ratio.replace(":", "x")}.png`;
        link.click();
        setStatus(`已导出 ${ratio} 图片`);
      } catch {
        setStatus("导出失败：图片可能来自跨域地址。");
      }
    }, 90);
  }

  async function save() {
    if (!baseImage) {
      setStatus("底图还没有加载完成");
      return;
    }
    if (!name.trim()) {
      setStatus("请填写模板名称");
      return;
    }
    setBusy(true);
    setStatus("");
    try {
      const image = await snapshot();
      await api(`/runs/${encodeURIComponent(saveRunId)}/brand-creatives`, {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          uri: original ? "" : target.kind === "asset" ? target.uri : "",
          reason: reason.trim(),
          prompt: prompt.trim(),
          image,
          base_image: baseSrc.startsWith("data:image/") ? baseSrc : "",
          layout,
          regions,
          parent_id: original?.id || null,
        }),
      });
      setStatus("已保存为品牌模板");
      notify?.(original ? "已保存新的模板版本" : "已保存品牌模板");
      close();
    } catch (e) {
      setStatus((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const pendingField = pendingRegion ? (
    <div className="brand-studio-pending" role="group" aria-label="新增批注">
      <Field label="这条批注要改什么">
        <input
          value={note}
          maxLength={500}
          placeholder="例如：标题压住了折叠接缝，移到左上墙面"
          onChange={(e) => setNote(e.target.value)}
        />
      </Field>
      <div className="brand-studio-inline-actions">
        <Button variant="outline" onClick={() => setPendingRegion(null)}>
          取消
        </Button>
        <Button disabled={!note.trim()} onClick={addRegion}>
          添加批注
        </Button>
      </div>
    </div>
  ) : null;

  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="brand-studio-overlay" />
        <Dialog.Content className="brand-studio" aria-describedby={undefined}>
          <header className="brand-studio-header">
            <div>
              <Dialog.Title asChild>
                <p>{original ? "用模板做新版" : "存为品牌模板"}</p>
              </Dialog.Title>
              <span>
                {original ? `母版：${original.name}` : "从当前素材建立可复用的版式"}
                {replaced ? " · 已替换底图" : ""}
              </span>
            </div>
            <div className="gallery-actions">
              <Button variant="outline" disabled={busy || !baseImage} onClick={() => download(layout.ratio)}>
                <Download size={14} />
                导出当前尺寸
              </Button>
              <Dialog.Close asChild>
                <Button size="icon" variant="ghost" aria-label="关闭品牌模板编辑器">
                  <X size={19} />
                </Button>
              </Dialog.Close>
            </div>
          </header>
          <div className="brand-studio-body">
            <div className="brand-studio-canvas" ref={attachContainer}>
              <div className="brand-studio-stage" data-testid="brand-studio-stage">
                {loading ? (
                  <div className="brand-studio-loading">
                    <LoaderCircle className="spin" size={24} />
                    正在读取素材
                  </div>
                ) : baseImage ? (
                  <Stage
                    ref={stageRef}
                    width={stageWidth}
                    height={stageHeight}
                    onMouseDown={startRegion}
                    onMouseMove={moveRegion}
                    onMouseUp={finishRegion}
                    onMouseLeave={finishRegion}
                    style={{ background: "#fff" }}
                  >
                    <Layer>
                      {crop ? (
                        <KonvaImage
                          image={baseImage}
                          crop={crop}
                          width={stageWidth}
                          height={stageHeight}
                          listening={mode === "annotate"}
                        />
                      ) : null}
                      {logoImage ? (
                        <KonvaImage
                          image={logoImage}
                          x={layout.logoX * stageWidth}
                          y={layout.logoY * stageHeight}
                          width={logoWidth}
                          height={logoHeight}
                          draggable={mode === "layout"}
                          listening={mode === "layout"}
                          onDragEnd={(event) => {
                            const node = event.target;
                            setLayout((value) => ({
                              ...value,
                              logoX: clamp(node.x() / stageWidth),
                              logoY: clamp(node.y() / stageHeight),
                            }));
                          }}
                        />
                      ) : null}
                      {layout.title.trim() ? (
                        <KonvaText
                          text={layout.title}
                          x={layout.titleX * stageWidth}
                          y={layout.titleY * stageHeight}
                          width={stageWidth * 0.84}
                          fontSize={titleSize}
                          lineHeight={1.16}
                          fontStyle="bold"
                          fill={layout.color}
                          fontFamily="system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif"
                          shadowColor="#00000055"
                          shadowBlur={layout.color === "#ffffff" ? 6 : 0}
                          draggable={mode === "layout"}
                          listening={mode === "layout"}
                          onDragEnd={(event) => {
                            const node = event.target;
                            setLayout((value) => ({
                              ...value,
                              titleX: clamp(node.x() / stageWidth),
                              titleY: clamp(node.y() / stageHeight),
                            }));
                          }}
                        />
                      ) : null}
                    </Layer>
                    <Layer listening={false} ref={regionLayerRef}>
                      {mode === "annotate" &&
                        regions.map((region, index) => (
                          <Rect
                            key={`${region.x}-${region.y}-${index}`}
                            x={region.x * stageWidth}
                            y={region.y * stageHeight}
                            width={region.width * stageWidth}
                            height={region.height * stageHeight}
                            stroke="#e5533d"
                            strokeWidth={2}
                            dash={[6, 4]}
                            fill="rgba(229,83,61,0.14)"
                          />
                        ))}
                      {draftRegion && (
                        <Rect
                          x={draftRegion.x * stageWidth}
                          y={draftRegion.y * stageHeight}
                          width={draftRegion.width * stageWidth}
                          height={draftRegion.height * stageHeight}
                          stroke="#e5533d"
                          strokeWidth={2}
                          dash={[4, 3]}
                          fill="rgba(229,83,61,0.1)"
                        />
                      )}
                    </Layer>
                  </Stage>
                ) : (
                  <div className="brand-studio-loading">
                    <ImageIcon size={26} />
                    {error || "暂无底图"}
                  </div>
                )}
              </div>
              <div className="brand-studio-toolbar" role="group" aria-label="编辑模式">
                <div className="brand-studio-tabs">
                  <button
                    type="button"
                    className={mode === "layout" ? "active" : ""}
                    aria-pressed={mode === "layout"}
                    onClick={() => setMode("layout")}
                  >
                    排版
                  </button>
                  <button
                    type="button"
                    className={mode === "annotate" ? "active" : ""}
                    aria-pressed={mode === "annotate"}
                    onClick={() => setMode("annotate")}
                  >
                    批注
                  </button>
                </div>
                <div className="brand-studio-ratios">
                  {ratios.map((item) => (
                    <Button
                      key={item.value}
                      size="default"
                      variant={layout.ratio === item.value ? "default" : "outline"}
                      onClick={() => setLayout((value) => ({ ...value, ratio: item.value }))}
                    >
                      {item.label}
                    </Button>
                  ))}
                </div>
              </div>
              <p className="brand-studio-hint">
                {mode === "annotate"
                  ? "在画面上拖出要修改的区域，再写一句修改说明。批注跟模板一起保存。"
                  : "拖动标题和 Logo 调整位置；用右侧的裁切滑块选择画面取景。"}
              </p>
            </div>
            <aside className="brand-studio-side">
              {original && (
                <section className="brand-studio-original">
                  <span>母版对比</span>
                  <img src={original.image} alt={`母版 ${original.name}`} />
                  {original.reason && <small>{original.reason}</small>}
                </section>
              )}
              <Field label="模板名称">
                <input
                  value={name}
                  maxLength={160}
                  placeholder="例如：鞋刷主图 · 侧光识别款"
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
              <Field label="为什么选它">
                <textarea
                  rows={2}
                  maxLength={2000}
                  value={reason}
                  placeholder="例如：柄尾标记和刷毛接触点都清楚，左上留白能放深色标题"
                  onChange={(e) => setReason(e.target.value)}
                />
              </Field>
              <div className="brand-studio-grid">
                <Field label="标题">
                  <input
                    value={layout.title}
                    maxLength={120}
                    placeholder="留空则不叠字"
                    onChange={(e) => setLayout((value) => ({ ...value, title: e.target.value }))}
                  />
                </Field>
                <Field label="标题颜色">
                  <select
                    value={layout.color}
                    onChange={(e) =>
                      setLayout((value) => ({
                        ...value,
                        color: e.target.value as BrandCreativeLayout["color"],
                      }))
                    }
                  >
                    <option value="#ffffff">白色</option>
                    <option value="#171717">深色</option>
                  </select>
                </Field>
              </div>
              <div className="brand-studio-grid">
                <label className="brand-studio-range">
                  <span>左右取景</span>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.01}
                    value={layout.cropX}
                    onChange={(e) =>
                      setLayout((value) => ({ ...value, cropX: Number(e.target.value) }))
                    }
                  />
                </label>
                <label className="brand-studio-range">
                  <span>上下取景</span>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.01}
                    value={layout.cropY}
                    onChange={(e) =>
                      setLayout((value) => ({ ...value, cropY: Number(e.target.value) }))
                    }
                  />
                </label>
              </div>
              <div className="brand-studio-actions">
                <label className="brand-studio-upload">
                  <Upload size={14} />
                  换一张新品底图
                  <input
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    onChange={(e) => {
                      void pickReplacement(e.target.files?.[0]);
                      e.target.value = "";
                    }}
                  />
                </label>
                <label className="brand-studio-upload">
                  <Upload size={14} />
                  {layout.logo ? "更换 Logo" : "叠加 Logo"}
                  <input
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    onChange={(e) => {
                      void pickLogo(e.target.files?.[0]);
                      e.target.value = "";
                    }}
                  />
                </label>
                {layout.logo && (
                  <Button
                    variant="outline"
                    onClick={() => setLayout((value) => ({ ...value, logo: "" }))}
                  >
                    移除 Logo
                  </Button>
                )}
              </div>
              <section className="brand-studio-regions" aria-label="批注列表">
                <div className="brand-studio-section-head">
                  <span>批注 {regions.length ? `(${regions.length})` : ""}</span>
                  {mode !== "annotate" && regions.length > 0 && (
                    <button type="button" onClick={() => setMode("annotate")}>
                      回到画面
                    </button>
                  )}
                </div>
                {pendingField}
                {regions.length ? (
                  <ul>
                    {regions.map((region, index) => (
                      <li key={`${index}-${region.note}`}>
                        <span>{region.note}</span>
                        <IconButton
                          icon={Trash2}
                          label={`删除批注 ${index + 1}`}
                          onClick={() =>
                            setRegions((items) => items.filter((_, itemIndex) => itemIndex !== index))
                          }
                        />
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">还没有批注。切到“批注”后在画面上拖出区域。</p>
                )}
              </section>
              <Field label="画面提示词">
                <textarea
                  rows={5}
                  maxLength={32000}
                  value={prompt}
                  placeholder="保存时会连同生成这张图的提示词一起留档"
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </Field>
              <div className="brand-studio-export">
                <span>出其他尺寸</span>
                <div>
                  {ratios.map((item) => (
                    <Button
                      key={item.value}
                      variant="outline"
                      disabled={!baseImage}
                      onClick={() => download(item.value)}
                    >
                      {item.value}
                    </Button>
                  ))}
                </div>
              </div>
              {error && (
                <div className="alert" role="alert">
                  {error}
                </div>
              )}
              <div className="brand-studio-footer">
                <span role="status">{status}</span>
                <Button
                  disabled={busy || loading || !baseImage || !name.trim() || !saveRunId}
                  onClick={() => void save()}
                  data-testid="brand-studio-save"
                >
                  {busy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}
                  {original ? "保存为新版本" : "保存品牌模板"}
                </Button>
              </div>
            </aside>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
