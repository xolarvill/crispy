import { useEffect, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Image,
  LoaderCircle,
  Play,
  X,
} from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
import { Button } from "./ui/button";
import { IconButton } from "./console-controls";
import { api, type Run, type RunMedia } from "../data";

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
}: {
  media: RunMedia;
  demo: boolean;
  full?: boolean;
}) {
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
          {full && <span>媒体无法加载</span>}
        </span>
      ) : media.kind === "image" ? (
        <img
          src={mediaUrl(media.uri, demo)}
          alt={full ? "任务媒体素材" : ""}
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
  const media = mediaFor(run),
    count = run.media_count ?? media.length;
  if (!media.length)
    return (
      <span className="empty-media" aria-label="暂无媒体" title="暂无媒体">
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
          aria-label={`预览 ${run.name || run.product_code} ${item.kind === "video" ? "视频" : "图片"} ${i + 1}，共 ${count} 份媒体`}
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
}: {
  run: Run;
  initial: RunMedia;
  demo: boolean;
  close: () => void;
  trigger: HTMLElement | null;
}) {
  const [items, setItems] = useState(() => mediaFor(run)),
    [uri, setUri] = useState(initial.uri),
    [loading, setLoading] = useState(!demo),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0);
  const index = Math.max(
      0,
      items.findIndex((item) => item.uri === uri),
    ),
    active = items[index];
  useEffect(() => {
    if (demo) return;
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
  }, [demo, run.id, retry]);
  function select(offset: number) {
    if (items.length)
      setUri(items[(index + offset + items.length) % items.length].uri);
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
                <p>{run.name || run.product_code || "任务媒体"}</p>
              </Dialog.Title>
              <span>
                {active?.source === "generated" ? "生成素材" : "参考素材"}
                {demo ? " · 演示" : ""}
              </span>
            </div>
            <div className="gallery-actions">
              {active && (
                <a
                  href={mediaUrl(active.uri, demo)}
                  target="_blank"
                  rel="noreferrer"
                  className="button button-ghost button-icon"
                  aria-label="打开原始媒体"
                  title="打开原始媒体"
                >
                  <ExternalLink size={16} />
                </a>
              )}
              <Dialog.Close asChild>
                <Button size="icon" variant="ghost" aria-label="关闭媒体预览">
                  <X size={19} />
                </Button>
              </Dialog.Close>
            </div>
          </div>
          {error && (
            <div className="gallery-error" role="alert">
              <span>媒体列表加载失败</span>
              <Button variant="outline" onClick={() => setRetry((r) => r + 1)}>
                重试
              </Button>
            </div>
          )}
          <div className="media-gallery-stage">
            {active ? (
              <MediaContent key={active.uri} media={active} demo={demo} full />
            ) : (
              <span className="muted">
                {loading ? "正在加载媒体" : "暂无媒体"}
              </span>
            )}
          </div>
          <div className="media-gallery-toolbar">
            <span>
              {loading ? (
                <>
                  <LoaderCircle size={13} className="spin" />
                  加载媒体列表
                </>
              ) : (
                `${items.length ? index + 1 : 0} / ${items.length}`
              )}
            </span>
            <div>
              <IconButton
                icon={ChevronLeft}
                label="上一份媒体"
                disabled={items.length < 2}
                onClick={() => select(-1)}
              />
              <IconButton
                icon={ChevronRight}
                label="下一份媒体"
                disabled={items.length < 2}
                onClick={() => select(1)}
              />
            </div>
          </div>
          {items.length > 1 && (
            <div className="gallery-filmstrip" aria-label="选择媒体">
              {items.map((item, i) => (
                <button
                  key={`${item.kind}-${item.uri}`}
                  type="button"
                  aria-label={`查看媒体 ${i + 1}`}
                  aria-pressed={index === i}
                  className={index === i ? "active" : ""}
                  onClick={() => setUri(item.uri)}
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
