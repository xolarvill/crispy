import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  LayoutTemplate,
  Maximize2,
  Image,
  LoaderCircle,
  Play,
  X,
} from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
import { Button } from "./ui/button";
import { IconButton } from "./console-controls";
import { api, type Run, type RunMedia } from "../data";
import { useI18n } from "../i18n";
export type MediaPreviewOptions = {
  complete?: boolean;
  onSelect?: (media: RunMedia) => void;
};
type OpenPreview = (
  run: Run,
  media: RunMedia,
  trigger: HTMLElement,
  options?: MediaPreviewOptions,
) => void;
function useSelectedMediaScroll(index: number) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const container = ref.current,
      selected = container?.children[index] as HTMLElement | undefined;
    if (!container || !selected) return;
    const left = selected.offsetLeft,
      right = left + selected.offsetWidth;
    if (left < container.scrollLeft) container.scrollLeft = left - 10;
    else if (right > container.scrollLeft + container.clientWidth)
      container.scrollLeft = right - container.clientWidth + 10;
  }, [index]);
  return ref;
}

export function mediaFor(run: Run): RunMedia[] {
  return (
    run.media_previews ??
    (run.image ? [{ uri: run.image, kind: "image", source: "input" }] : [])
  );
}
function mediaUrl(uri: string, demo: boolean) {
  if (/^https?:\/\//i.test(uri)) return uri;
  return demo && uri.startsWith("/")
    ? uri
    : `/api/media?path=${encodeURIComponent(uri)}`;
}
function MediaContent({
  media,
  demo,
  full = false,
  showError = false,
}: {
  media: RunMedia;
  demo: boolean;
  full?: boolean;
  showError?: boolean;
}) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false),
    [visible, setVisible] = useState(full),
    ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    setFailed(false);
  }, [media.uri]);
  useEffect(() => {
    if (full || media.kind !== "video" || !ref.current) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "80px" },
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [full, media.kind]);
  return (
    <span ref={ref} className={`media-content ${full ? "full" : ""}`}>
      {failed ? (
        <span className="media-fallback">
          <Image size={full ? 32 : 18} />
          {(full || showError) && (
            <span>{t("Media could not load", "媒体无法加载")}</span>
          )}
        </span>
      ) : media.kind === "image" ? (
        <img
          src={mediaUrl(media.uri, demo)}
          alt={full ? t("Task media", "任务媒体素材") : ""}
          loading={full ? "eager" : "lazy"}
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : (
        <>
          {visible && (
            <video
              key={media.uri}
              src={mediaUrl(media.uri, demo)}
              controls={full}
              muted={!full}
              playsInline
              preload="metadata"
              onError={() => setFailed(true)}
              onLoadedMetadata={(event) => {
                if (!full && Number.isFinite(event.currentTarget.duration))
                  event.currentTarget.currentTime = Math.min(
                    0.1,
                    event.currentTarget.duration / 2,
                  );
              }}
            />
          )}
          {!full && (
            <span className="video-indicator">
              <Play size={15} fill="currentColor" />
            </span>
          )}
        </>
      )}
    </span>
  );
}
export function RunMediaStrip({
  run,
  demo,
  open,
}: {
  run: Run;
  demo: boolean;
  open: (run: Run, media: RunMedia, trigger: HTMLElement) => void;
}) {
  const { t, locale } = useI18n();
  const media = mediaFor(run),
    count = run.media_count ?? media.length;
  if (!media.length)
    return (
      <span
        className="empty-media"
        aria-label={t("No media", "暂无媒体")}
        title={t("No media", "暂无媒体")}
      >
        <Image size={18} />
      </span>
    );
  return (
    <div className="run-media-strip">
      {media.slice(0, 3).map((item, i) => (
        <button
          key={`${item.kind}-${item.uri}`}
          type="button"
          className="run-media-thumb"
          aria-label={
            locale === "zh"
              ? `预览 ${run.name || run.product_code} ${item.kind === "video" ? "视频" : "图片"} ${i + 1}，共 ${count} 份媒体`
              : `Preview ${run.name || run.product_code} ${item.kind === "video" ? "video" : "image"} ${i + 1} of ${count}`
          }
          onClick={(event) => open(run, item, event.currentTarget)}
        >
          <MediaContent media={item} demo={demo} />
          {i === 0 && count > 1 && (
            <span className="mobile-media-count">{count}</span>
          )}
          {i === 2 && count > 3 && (
            <span className="more-media-count">+{count - 3}</span>
          )}
        </button>
      ))}
    </div>
  );
}
export function RunMediaGallery({
  run,
  initial,
  demo,
  close,
  trigger,
  complete = false,
  onSelect,
  saveTemplate,
}: {
  run: Run;
  initial: RunMedia;
  demo: boolean;
  close: () => void;
  trigger: HTMLElement | null;
  complete?: boolean;
  onSelect?: (media: RunMedia) => void;
  saveTemplate?: (run: Run, media: RunMedia) => void;
}) {
  const { t } = useI18n();
  const [items, setItems] = useState(() => mediaFor(run)),
    [uri, setUri] = useState(initial.uri),
    [loading, setLoading] = useState(!demo && !complete),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0);
  const index = Math.max(
      0,
      items.findIndex((item) => item.uri === uri),
    ),
    active = items[index];
  const filmstrip = useSelectedMediaScroll(index);
  useEffect(() => {
    if (demo || complete) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<{ media_previews: RunMedia[] }>(
      `/runs/${encodeURIComponent(run.id)}/media`,
      { signal: controller.signal },
    )
      .then((result) => setItems(result.media_previews))
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [demo, run.id, retry, complete]);
  const touch = useRef<{ x: number; y: number } | null>(null);
  function choose(media: RunMedia) {
    setUri(media.uri);
    onSelect?.(media);
  }
  function select(offset: number) {
    if (items.length)
      choose(items[(index + offset + items.length) % items.length]);
  }
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="media-gallery-overlay" />
        <Dialog.Content
          className="media-gallery"
          aria-describedby={undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (trigger?.isConnected) trigger.focus();
            else document.getElementById("task-search")?.focus();
          }}
          onKeyDown={(event) => {
            if ((event.target as HTMLElement).closest("video")) return;
            if (event.key === "ArrowRight") {
              event.preventDefault();
              select(1);
            }
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              select(-1);
            }
          }}
        >
          <div className="media-gallery-header">
            <div>
              <Dialog.Title asChild>
                <p>{run.name || run.product_code || t("Task media", "任务媒体")}</p>
              </Dialog.Title>
              <span>
                {active?.source === "generated"
                  ? t("Generated assets", "生成素材")
                  : t("Reference assets", "参考素材")}
                {demo ? t(" · Demo", " · 演示") : ""}
              </span>
            </div>
            <div className="gallery-actions">
              {!demo && saveTemplate && active?.kind === "image" && (
                <Button
                  variant="outline"
                  onClick={() => saveTemplate(run, active)}
                  data-testid="save-brand-template"
                >
                  <LayoutTemplate size={14} />
                  {t("Save as brand template", "存为品牌模板")}
                </Button>
              )}
              <Dialog.Close asChild>
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={t("Close media preview", "关闭媒体预览")}
                >
                  <X size={19} />
                </Button>
              </Dialog.Close>
            </div>
          </div>
          {error && (
            <div className="gallery-error" role="alert">
              <span>{t("Could not load the media list", "媒体列表加载失败")}</span>
              <Button variant="outline" onClick={() => setRetry((r) => r + 1)}>
                {t("Retry", "重试")}
              </Button>
            </div>
          )}
          <div
            className="media-gallery-stage"
            onTouchStart={(event) => {
              if (event.touches.length !== 1) {
                touch.current = null;
                return;
              }
              if (active?.kind === "video") return;
              const point = event.touches[0];
              if (point) touch.current = { x: point.clientX, y: point.clientY };
            }}
            onTouchMove={(event) => {
              if (event.touches.length !== 1) touch.current = null;
            }}
            onTouchCancel={() => {
              touch.current = null;
            }}
            onTouchEnd={(event) => {
              const start = touch.current;
              touch.current = null;
              const end = event.changedTouches[0];
              if (!start || !end || active?.kind === "video") return;
              const dx = end.clientX - start.x,
                dy = end.clientY - start.y;
              if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.4)
                select(dx < 0 ? 1 : -1);
            }}
          >
            {active ? (
              <MediaContent key={active.uri} media={active} demo={demo} full />
            ) : (
              <span className="muted">
                {loading
                  ? t("Loading media", "正在加载媒体")
                  : t("No media", "暂无媒体")}
              </span>
            )}
          </div>
          <div className="media-gallery-toolbar">
            <span aria-live="polite" aria-atomic="true">
              {loading ? (
                <>
                  <LoaderCircle size={13} className="spin" />
                  {t("Loading media list", "加载媒体列表")}
                </>
              ) : (
                `${items.length ? index + 1 : 0} / ${items.length}`
              )}
            </span>
            <div>
              <IconButton
                icon={ChevronLeft}
                label={t("Previous media", "上一份媒体")}
                disabled={items.length < 2}
                onClick={() => select(-1)}
              />
              <IconButton
                icon={ChevronRight}
                label={t("Next media", "下一份媒体")}
                disabled={items.length < 2}
                onClick={() => select(1)}
              />
            </div>
          </div>
          {items.length > 1 && (
            <div
              className="gallery-filmstrip"
              role="group"
              ref={filmstrip}
              aria-label={t("Choose media", "选择媒体")}
            >
              {items.map((item, i) => (
                <button
                  key={`${item.kind}-${item.uri}`}
                  type="button"
                  aria-label={t(`View media ${i + 1}`, `查看媒体 ${i + 1}`)}
                  aria-pressed={index === i}
                  className={index === i ? "active" : ""}
                  onClick={() => choose(item)}
                >
                  <MediaContent media={item} demo={demo} />
                </button>
              ))}
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function TaskAssetWidget({
  run,
  demo,
  refreshKey = 0,
  open,
}: {
  run: Run;
  demo: boolean;
  refreshKey?: number;
  open: OpenPreview;
}) {
  const { t } = useI18n();
  const [items, setItems] = useState(() => mediaFor(run)),
    [uri, setUri] = useState(() => mediaFor(run)[0]?.uri || ""),
    [loading, setLoading] = useState(!demo),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0),
    [complete, setComplete] = useState(demo),
    [expanded, setExpanded] = useState(false);
  const strip = useRef<HTMLDivElement>(null),
    index = Math.max(
      0,
      items.findIndex((item) => item.uri === uri),
    ),
    active = items[index],
    generated = items.filter((item) => item.source === "generated").length;
  useEffect(() => {
    if (demo) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<{ media_previews: RunMedia[] }>(
      `/runs/${encodeURIComponent(run.id)}/media`,
      { signal: controller.signal },
    )
      .then((result) => {
        setItems(result.media_previews);
        setComplete(true);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [demo, run.id, refreshKey, retry]);
  useEffect(() => {
    if (!expanded) return;
    const container = strip.current,
      selected = container?.children[index] as HTMLElement | undefined;
    if (!container || !selected) return;
    const left = selected.offsetLeft,
      right = left + selected.offsetWidth;
    if (left < container.scrollLeft) container.scrollLeft = left - 10;
    else if (right > container.scrollLeft + container.clientWidth)
      container.scrollLeft = right - container.clientWidth + 10;
  }, [index, expanded]);
  function enlarge(media: RunMedia, trigger: HTMLElement) {
    open(
      { ...run, media_previews: items, media_count: items.length },
      media,
      trigger,
      { complete, onSelect: (item) => setUri(item.uri) },
    );
  }
  const meta = loading
    ? t("Loading assets", "正在加载素材")
    : items.length
      ? t(
          `${items.length} assets${generated ? ` · ${generated} generated` : ""}`,
          `${items.length} 个素材${generated ? ` · ${generated} 个生成` : ""}`,
        )
      : t("No media assets", "暂无媒体素材");
  return (
    <section
      className="task-asset-widget"
      data-expanded={expanded}
      aria-label={t("Task assets", "任务素材")}
    >
      <div className="task-asset-bar">
        <button
          type="button"
          className="task-asset-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <span className="task-asset-icon" aria-hidden="true">
            {loading ? (
              <LoaderCircle size={14} className="spin" />
            ) : (
              <Image size={14} />
            )}
          </span>
          <span className="task-asset-title">
            {t("Task assets", "任务素材")}
          </span>
          <span className="task-asset-meta" aria-live="polite">
            {meta}
          </span>
          <ChevronDown size={14} className="task-asset-chevron" />
        </button>
        {items.length > 0 && (
          <div
            className="task-asset-thumbs"
            role="group"
            aria-label={t("Task asset thumbnails", "任务素材缩略图")}
          >
            {items.slice(0, 6).map((item, i) => (
              <button
                type="button"
                key={`${item.kind}-${item.uri}`}
                className={`task-asset-thumb${index === i ? " active" : ""}`}
                aria-label={t(
                  `Open task asset ${i + 1}`,
                  `打开任务素材 ${i + 1}`,
                )}
                onClick={(event) => {
                  setUri(item.uri);
                  enlarge(item, event.currentTarget);
                }}
              >
                <MediaContent media={item} demo={demo} />
              </button>
            ))}
            {items.length > 6 && (
              <span className="task-asset-more">+{items.length - 6}</span>
            )}
          </div>
        )}
      </div>
      {error && (
        <div className="gallery-error" role="alert">
          <span>{t("Media could not load", "媒体加载失败")}</span>
          <Button variant="outline" onClick={() => setRetry((r) => r + 1)}>
            {t("Retry", "重试")}
          </Button>
        </div>
      )}
      {expanded &&
        (active ? (
          <div className="task-asset-body">
            <button
              type="button"
              className="task-asset-preview"
              aria-label={t(
                `Enlarge ${active.kind === "video" ? "video" : "image"} ${index + 1}`,
                `放大查看${active.kind === "video" ? "视频" : "图片"} ${index + 1}`,
              )}
              onClick={(event) => enlarge(active, event.currentTarget)}
            >
              <MediaContent key={active.uri} media={active} demo={demo} showError />
              <span className="detail-media-expand">
                <Maximize2 size={17} />
              </span>
            </button>
            <div
              className="task-asset-strip"
              role="group"
              ref={strip}
              aria-label={t("Task asset thumbnails", "任务素材缩略图")}
            >
              {items.map((item, i) => (
                <button
                  type="button"
                  key={`${item.kind}-${item.uri}`}
                  className={index === i ? "active" : ""}
                  aria-label={t(
                    `Choose task asset ${i + 1}`,
                    `选择任务素材 ${i + 1}`,
                  )}
                  aria-pressed={index === i}
                  onClick={() => setUri(item.uri)}
                >
                  <MediaContent media={item} demo={demo} />
                  <span>{i + 1}</span>
                </button>
              ))}
            </div>
            <div className="reference-footer">
              <Image size={14} />
              <span>
                {`${active.source === "generated" ? t("Generated assets", "生成素材") : t("Reference assets", "参考素材")}${active.kind === "video" ? t(" · Video", " · 视频") : ""}${demo ? t(" · Demo", " · 演示") : ""}`}
              </span>
            </div>
          </div>
        ) : (
          <div className="material-empty">
            {loading ? (
              <LoaderCircle className="spin" size={24} />
            ) : (
              <Image size={24} />
            )}
            <span>
              {loading
                ? t("Loading assets", "正在加载素材")
                : t("No media assets", "暂无媒体素材")}
            </span>
          </div>
        ))}
    </section>
  );
}
