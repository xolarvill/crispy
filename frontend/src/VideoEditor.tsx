import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {
  DEFAULT_ELEMENT_COLORS,
  PlayerControls,
  TimelineManager,
  useTimelineControl,
} from "@twick/video-editor";
import {
  TimelineProvider,
  TrackElement,
  useTimelineContext,
} from "@twick/timeline";
import {
  LivePlayerProvider,
  useLivePlayerContext,
  PLAYER_STATE,
} from "@twick/live-player";
import {
  Check,
  ArrowLeft,
  Copy,
  Download,
  Film,
  History,
  LoaderCircle,
  Plus,
  Save,
  Type,
  Upload,
  X,
  Music,
} from "lucide-react";
import { api, type RunMedia } from "./data";
import { useI18n } from "./i18n";
import { Button } from "./components/ui/button";
import {
  appendTrack,
  clipCount,
  clipEnd,
  clipTimingLimits,
  cutClip,
  durationUs,
  findClip,
  frameTimeUs,
  fromTwick,
  MAX_VIDEO_CLIPS,
  MAX_VIDEO_TIME_US,
  MAX_VIDEO_TRACKS,
  newClip,
  removeClip,
  toTwick,
  videoId,
  videoMediaUrl,
  type BoardVideoLayer,
  type VideoClip,
  type VideoDocument,
  type VideoJob,
  type VideoProject,
  type VideoSource,
  type VideoTarget,
} from "./video-data";
import "./video-twick.generated.css";
import "./video.css";

const json = (value: unknown) => JSON.stringify(value);
const request = () => crypto.randomUUID();
const seconds = (us: number) => Math.round(us / 1000) / 1000;

type ExportReview = {
  revision: number;
  sheet_uri: string;
  notes: string[];
  checks: { check: string; status: "PASS" | "WARN" | "FAIL"; value: string; expected: string; reason?: string }[];
};

type RunSourceImport = VideoProject & { imported_source_id: string };
type BoardComposition = {
  layers: BoardVideoLayer[];
  pinOutput: (uri: string) => Promise<void>;
};

function withOverlay(document: VideoDocument, clip: VideoClip, name: string): VideoDocument {
  clip.duration_us = durationUs(document) || 3_000_000;
  return {
    ...document,
    tracks: [...document.tracks, { id: videoId("t-overlay"), name, clips: [clip] }],
  };
}

export default function VideoStudio({
  target,
  close,
  notify,
  board,
}: {
  target: VideoTarget;
  close: () => void;
  notify?: (message: string) => void;
  board?: BoardComposition;
}) {
  const { t } = useI18n();
  const [project, setProject] = useState<VideoProject | null>(null);
  const [seed, setSeed] = useState<VideoDocument | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [projects, setProjects] = useState<
    Pick<VideoProject, "id" | "title" | "revision">[]
  >([]);
  const [runAssets, setRunAssets] = useState<RunMedia[]>([]);
  const [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false),
    [remote, setRemote] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<
    { revision: number; actor: string; summary: string }[] | null
  >(null);
  const [reviewJobId, setReviewJobId] = useState<string | null>(null);
  const [job, setJob] = useState<VideoJob | null>(null);
  const [renderReady, setRenderReady] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [pinnedUri, setPinnedUri] = useState<string | null>(null);
  const draft = useRef<VideoDocument | null>(null),
    saved = useRef(""),
    dirtyRef = useRef(false);
  const importRef = useRef<HTMLInputElement>(null);
  const initialization = useRef<Promise<VideoProject> | null>(null);
  useEffect(() => {
    api<{ ready: boolean }>("/video-editing/capabilities")
      .then((result) => {
        setRenderReady(result.ready);
        if (!result.ready)
          setError(
            t(
              "Install FFmpeg and FFprobe to enable local export.",
              "安装 FFmpeg 和 FFprobe 后即可在本地导出。",
            ),
          );
      })
      .catch((e) => setError(e.message));
  }, [t]);
  const task = useCallback((p: VideoProject) => {
    setProject(p);
    setSeed(p.document);
    draft.current = p.document;
    saved.current = json(p.document);
    dirtyRef.current = false;
    setDirty(false);
    setRemote(false);
    setEpoch((v) => v + 1);
    setError("");
  }, []);
  const refreshList = useCallback(
    async () => setProjects(await api(`/runs/${target.runId}/video-projects`)),
    [target.runId],
  );

  useEffect(() => {
    let active = true;
    if (!initialization.current)
      initialization.current = (async () => {
        const list = target.boardKey ? [] : await api<VideoProject[]>(
          `/runs/${target.runId}/video-projects`,
        );
        if (target.projectId || list.length)
          return api<VideoProject>(
            `/video-projects/${target.projectId || list[0].id}`,
          );
        let p = await api<VideoProject>(
          `/runs/${target.runId}/video-projects`,
          {
            method: "POST",
            body: json({
              title: target.boardKey
                ? `Board · ${(target.uri?.split(/[\\/]/).pop() || "Video").slice(0, 130)}`
                : t("Video edit", "视频剪辑"),
              request_id: target.boardKey || request(),
            }),
          },
        );
        // Revision 1 can be the source import from a previously interrupted setup.
        if (target.uri && p.revision <= 1 && !p.document.tracks.length) {
          const imported = await api<RunSourceImport>(`/video-projects/${p.id}/sources/from-run`, {
            method: "POST",
            body: json({
              uri: target.uri,
              expected_revision: 0,
              request_id: "initialize-source",
            }),
          });
          p = imported;
          const source = p.sources.find((item) => item.id === imported.imported_source_id);
          if (!source) throw new Error(t("Source could not be imported", "无法导入源素材"));
          if (p.revision > 1) return p;
          const doc = {
            ...p.document,
            tracks: [
              {
                id: "t-picture-initial",
                name: "Video",
                clips: [{ ...newClip(source.kind, source), id: "initial-clip" }],
              },
            ],
          };
          p = await api(`/video-projects/${p.id}`, {
            method: "PUT",
            body: json({
              document: doc,
              expected_revision: p.revision,
              request_id: "initialize-timeline",
              summary: "Start from task media",
            }),
          });
        }
        return p;
      })();
    initialization.current
      .then((p) => {
        if (active) {
          task(p);
          void refreshList().catch((error: Error) => {
            if (active) setError(error.message);
          });
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [target, task, refreshList, t]);

  useEffect(() => {
    if (!project) return;
    const controller = new AbortController();
    api<RunMedia[]>(`/video-projects/${project.id}/run-assets`, {
      signal: controller.signal,
    })
      .then(setRunAssets)
      .catch(() => {});
    return () => controller.abort();
  }, [project?.id]);

  useEffect(() => {
    if (!project) return;
    let active = true;
    api<VideoJob[]>(`/video-projects/${project.id}/renders`)
      .then((jobs) => {
        if (active) setJob((current) => current || jobs[0] || null);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [project?.id]);

  useEffect(() => {
    if (!project || busy) return;
    let active = true;
    const id = window.setInterval(async () => {
      try {
        const latest = await api<VideoProject>(`/video-projects/${project.id}`);
        if (active && latest.revision !== project.revision) {
          if (dirtyRef.current) setRemote(true);
          else task(latest);
        }
      } catch {
        /* Saving still surfaces connection failures. */
      }
    }, 4000);
    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, [project?.id, project?.revision, busy, task]);

  useEffect(() => {
    if (!dirty) return;
    const prevent = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty]);

  useEffect(() => {
    if (!project || !job || !["queued", "running"].includes(job.status)) return;
    let active = true;
    const id = window.setInterval(() => {
      api<VideoJob>(`/video-projects/${project.id}/renders/${job.id}`)
        .then((next) => {
          if (!active) return;
          setJob(next);
          if (next.status === "completed")
            notify?.(t("Video exported to task assets", "视频已导出到任务素材"));
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    }, 1000);
    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, [project?.id, job?.id, job?.status, notify, t]);

  const onChange = useCallback((doc: VideoDocument) => {
    draft.current = doc;
    const changed = json(doc) !== saved.current;
    dirtyRef.current = changed;
    setDirty(changed);
  }, []);

  async function save(): Promise<VideoProject> {
    if (!project || !draft.current) throw new Error("Project is not loaded");
    if (!dirtyRef.current) return project;
    const snapshot = draft.current;
    const p = await api<VideoProject>(`/video-projects/${project.id}`, {
      method: "PUT",
      body: json({
        document: snapshot,
        expected_revision: project.revision,
        request_id: request(),
        summary: "Edit video timeline",
      }),
    });
    setProject(p);
    setProjects((list) =>
      list.map((item) =>
        item.id === p.id ? { ...item, revision: p.revision } : item,
      ),
    );
    saved.current = json(p.document);
    dirtyRef.current = json(draft.current) !== saved.current;
    setDirty(dirtyRef.current);
    setRemote(false);
    return p;
  }

  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function importSource(uri?: string, file?: File) {
    await perform(async () => {
      const p = await save();
      let next: VideoProject;
      if (file) {
        const body = new FormData();
        body.set("file", file);
        body.set("expected_revision", String(p.revision));
        body.set("request_id", request());
        next = await api(`/video-projects/${p.id}/sources`, {
          method: "POST",
          body,
        });
      } else
        next = await api(`/video-projects/${p.id}/sources/from-run`, {
          method: "POST",
          body: json({
            uri,
            expected_revision: p.revision,
            request_id: request(),
          }),
        });
      task(next);
    });
  }

  async function addBoardImage(layer: Extract<BoardVideoLayer, { kind: "image" }>) {
    await perform(async () => {
      if (!project || !draft.current) return;
      const current = draft.current;
      if (current.tracks.length >= MAX_VIDEO_TRACKS || clipCount(current) >= MAX_VIDEO_CLIPS)
        throw new Error(t("The timeline has reached its layer or clip limit.", "时间线已达到图层或片段上限。"));
      const imported = await api<RunSourceImport>(`/video-projects/${project.id}/sources/from-run`, {
        method: "POST",
        body: json({ uri: layer.uri, expected_revision: project.revision, request_id: request() }),
      });
      const source = imported.sources.find((item) => item.id === imported.imported_source_id);
      const overlay = source?.kind === "image"
        ? { ...newClip("image", source), x: 0.05, y: 0.05, width: 0.3, height: 0.3 }
        : null;
      const document = overlay ? withOverlay(current, overlay, layer.label.slice(0, 100)) : current;
      task(imported);
      setSeed(document);
      onChange(document);
      if (!overlay) throw new Error(t("This board asset is not a supported image.", "此白板素材无法作为图片叠加。"));
    });
  }

  async function openProject(id: string) {
    await perform(async () => {
      await save();
      task(await api(`/video-projects/${id}`));
      setHistory(null);
      setJob(null);
    });
  }

  const dismiss = () => {
    if (busy) return;
    if (dirtyRef.current) setDiscard(true);
    else close();
  };
  const rendering = job && ["queued", "running"].includes(job.status);
  const outputUri = job?.status === "completed" ? job.uri : undefined;
  const body = (
    <>
          <header className="video-header">
            <div className="video-heading">
              {board ? (
                <Button variant="ghost" size="icon" onClick={dismiss} disabled={busy}
                  autoFocus aria-label={t("Back to board", "返回白板")}>
                  <ArrowLeft size={18} />
                </Button>
              ) : <Film size={20} />}
              <div>
                {board ? <p>{t("Video composition", "视频合成")}</p> : (
                  <Dialog.Title asChild>
                    <p>{project?.title || t("Video editor", "视频编辑")}</p>
                  </Dialog.Title>
                )}
                <span>
                  {dirty
                    ? t("Unsaved changes", "有未保存的修改")
                    : project
                      ? `${t("Saved", "已保存")} · r${project.revision}`
                      : t("Opening project…", "正在打开工程…")}
                </span>
              </div>
            </div>
            <div className="video-header-actions">
              <Button
                variant="ghost"
                disabled={!project || busy}
                onClick={() =>
                  perform(async () => {
                    await navigator.clipboard.writeText(
                      `Crispy video project ${project!.id}\nRun: ${project!.run_id}\nRead get_video_project before editing. Times are microseconds; preserve human edits.\n${window.location.origin}/?mode=live#/assets?video=${project!.id}&run=${project!.run_id}`,
                    );
                    notify?.(
                      t("Agent handoff copied", "已复制给 agent 的工程信息"),
                    );
                  })
                }
              >
                <Copy size={14} />
                {t("Agent", "交给 Agent")}
              </Button>
              <Button
                variant="ghost"
                disabled={!project || busy}
                onClick={() =>
                  perform(async () => {
                    setReviewJobId(null);
                    setHistory(await api(`/video-projects/${project!.id}/history`));
                  })
                }
              >
                <History size={15} />
                {t("History", "历史")}
              </Button>
              <Button
                variant="outline"
                disabled={!project || !dirty || busy || remote}
                onClick={() =>
                  perform(async () => {
                    await save();
                    notify?.(t("Video project saved", "视频工程已保存"));
                  })
                }
              >
                <Save size={14} />
                {t("Save", "保存")}
              </Button>
              <Button
                disabled={
                  !project || busy || !!rendering || remote || !renderReady
                }
                onClick={() =>
                  perform(async () => {
                    const p = await save();
                    setJob(
                      await api(`/video-projects/${p.id}/renders`, {
                        method: "POST",
                        body: json({
                          expected_revision: p.revision,
                          request_id: request(),
                        }),
                      }),
                    );
                  })
                }
              >
                {rendering ? (
                  <LoaderCircle size={14} className="spin" />
                ) : (
                  <Download size={14} />
                )}
                {t("Export MP4", "导出 MP4")}
              </Button>
              {!board && <Button
                variant="ghost"
                size="icon"
                aria-label={t("Close video editor", "关闭视频编辑器")}
                onClick={dismiss}
              >
                <X size={18} />
              </Button>}
            </div>
          </header>
          {error && (
            <div className="video-notice error" role="alert">
              {error}
              <button
                onClick={() => setError("")}
                aria-label={t("Dismiss error", "关闭错误")}
              >
                <X size={14} />
              </button>
            </div>
          )}
          {remote && (
            <div className="video-notice" role="alert">
              {t(
                "This project changed elsewhere. Your edits are kept here; load the latest revision to continue.",
                "工程已被其他窗口或 agent 修改。当前草稿仍保留，请加载最新版本后继续。",
              )}
              <Button
                variant="outline"
                onClick={() =>
                  perform(async () =>
                    task(await api(`/video-projects/${project!.id}`)),
                  )
                }
              >
                {t("Load latest", "加载最新版本")}
              </Button>
            </div>
          )}
          {job && (
            <div
              className={`video-export-bar ${job.status === "failed" ? "error" : ""}`}
              role="status"
            >
              {rendering ? (
                <>
                  <LoaderCircle size={14} className="spin" />
                  <span>
                    {t("Exporting", "正在导出")} r{job.revision} ·{" "}
                    {Math.round(job.progress * 100)}%
                  </span>
                  <progress max={1} value={job.progress} />
                  <Button
                    variant="ghost"
                    onClick={() =>
                      perform(async () =>
                        setJob(
                          await api(
                            `/video-projects/${project!.id}/renders/${job.id}/cancel`,
                            { method: "POST" },
                          ),
                        ),
                      )
                    }
                  >
                    {t("Cancel", "取消")}
                  </Button>
                </>
              ) : job.status === "completed" ? (
                <>
                  <Check size={15} />
                  <span>
                    {t("Exported to task assets", "已导出到任务素材")} · r
                    {job.revision}
                  </span>
                  <a
                    href={videoMediaUrl(job.uri)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t("Open video", "打开成片")}
                  </a>
                  <a href={videoMediaUrl(job.uri)} download="crispy-video.mp4">
                    {t("Download", "下载")}
                  </a>
                  <Button variant="outline" onClick={() => {
                    setHistory(null);
                    setReviewJobId(job.id);
                  }}>
                    {t("Review export", "检查成片")}
                  </Button>
                  {board && outputUri && (
                    <Button variant="outline" disabled={busy || pinnedUri === outputUri}
                      onClick={() => perform(async () => {
                        await board.pinOutput(outputUri);
                        setPinnedUri(outputUri);
                      })}>
                      <Plus size={14} />
                      {pinnedUri === outputUri ? t("On board", "已放到白板") : t("Add to board", "放到白板")}
                    </Button>
                  )}
                </>
              ) : (
                <span>{job.error || t("Export cancelled", "导出已取消")}</span>
              )}
              {!rendering && (
                <button
                  onClick={() => setJob(null)}
                  aria-label={t("Dismiss export status", "关闭导出状态")}
                >
                  <X size={14} />
                </button>
              )}
            </div>
          )}
          {loading ? (
            <div className="video-loading">
              <LoaderCircle className="spin" />
              {t("Preparing your workspace…", "正在准备编辑工作区…")}
            </div>
          ) : project && seed ? (
            <LivePlayerProvider>
              <TimelineProvider
                key={`${project.id}:${epoch}`}
                contextId={`crispy-${project.id}-${epoch}`}
                initialData={toTwick(seed, project.sources)}
                resolution={{ width: seed.width, height: seed.height }}
                analytics={{ enabled: false }}
              >
                <EditorBody
                  initial={seed}
                  sources={project.sources}
                  projectId={project.id}
                  onChange={onChange}
                  busy={busy}
                  boardLayers={board?.layers}
                  addBoardImage={addBoardImage}
                  library={
                    <>
                      <div className="video-library-head">
                        <h3>{t("Media", "素材")}</h3>
                        <Button
                          size="icon"
                          variant="ghost"
                          disabled={busy}
                          aria-label={t("Import media", "导入素材")}
                          onClick={() => importRef.current?.click()}
                        >
                          <Upload size={16} />
                        </Button>
                      </div>
                      <label className="video-project-picker">
                        {t("Project", "工程")}
                        <select
                          aria-label={t("Project", "工程")}
                          disabled={busy}
                          value={project.id}
                          onChange={(e) => openProject(e.target.value)}
                        >
                          {projects.map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.title} · r{p.revision}
                            </option>
                          ))}
                        </select>
                      </label>
                      {!board && <Button
                        variant="outline"
                        disabled={busy}
                        onClick={() =>
                          perform(async () => {
                            await save();
                            const p = await api<VideoProject>(
                              `/runs/${target.runId}/video-projects`,
                              {
                                method: "POST",
                                body: json({
                                  title: `${t("Video edit", "视频剪辑")} ${projects.length + 1}`,
                                  request_id: request(),
                                }),
                              },
                            );
                            task(p);
                            await refreshList();
                          })
                        }
                      >
                        <Plus size={14} />
                        {t("New edit", "新建剪辑")}
                      </Button>}
                      <div className="video-library-label">
                        {t("Project sources", "工程素材")}
                      </div>
                      <SourceLibrary sources={project.sources} />
                      <button
                        className="video-upload"
                        disabled={busy}
                        onClick={() => importRef.current?.click()}
                      >
                        <Upload size={18} />
                        <span>
                          {t(
                            "Import video, image or audio",
                            "导入视频、图片或音频",
                          )}
                        </span>
                        <small>
                          {t(
                            "Local files · up to 500 MB",
                            "本地文件 · 最大 500 MB",
                          )}
                        </small>
                      </button>
                      {!!runAssets.filter(
                        (a) =>
                          !project.sources.some(
                            (s) => s.name === a.uri.split(/[\\/]/).pop(),
                          ),
                      ).length && (
                        <details className="video-task-sources">
                          <summary>
                            {t("Add from this task", "从当前任务添加")}
                          </summary>
                          {runAssets.map((asset, i) => (
                            <button
                              key={asset.uri}
                              disabled={busy}
                              onClick={() => importSource(asset.uri)}
                            >
                              <Plus size={13} />
                              {asset.uri.split(/[\\/]/).pop() ||
                                `${t("Asset", "素材")} ${i + 1}`}
                            </button>
                          ))}
                        </details>
                      )}
                    </>
                  }
                />
              </TimelineProvider>
            </LivePlayerProvider>
          ) : (
            <div className="video-loading">
              {t("Could not open project", "无法打开工程")}
              <Button variant="outline" onClick={close}>
                {t("Close", "关闭")}
              </Button>
            </div>
          )}
          <input
            ref={importRef}
            type="file"
            hidden
            accept="video/mp4,video/quicktime,video/webm,image/png,image/jpeg,image/webp,audio/*"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void importSource(undefined, file);
              e.target.value = "";
            }}
          />
          {history && (
            <div
              className="video-history"
              role="dialog"
              aria-label={t("Project history", "工程历史")}
            >
              <div>
                <h3>{t("Project history", "工程历史")}</h3>
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => setHistory(null)}
                  aria-label={t("Close history", "关闭历史")}
                >
                  <X size={16} />
                </Button>
              </div>
              {history.map((h) => (
                <div key={h.revision}>
                  <section>
                    <strong>
                      r{h.revision} · {h.summary}
                    </strong>
                    <small>{h.actor}</small>
                  </section>
                  <Button
                    variant="outline"
                    disabled={busy || h.revision === project?.revision}
                    onClick={() =>
                      perform(async () => {
                        const p = await save();
                        task(
                          await api(`/video-projects/${p.id}/restore`, {
                            method: "POST",
                            body: json({
                              expected_revision: p.revision,
                              revision: h.revision,
                              request_id: request(),
                            }),
                          }),
                        );
                        setHistory(null);
                      })
                    }
                  >
                    {t("Restore", "恢复")}
                  </Button>
                </div>
              ))}
            </div>
          )}
          {project && job?.status === "completed" && reviewJobId === job.id && (
            <VideoExportReview
              key={job.id}
              projectId={project.id}
              job={job}
              close={() => setReviewJobId(null)}
            />
          )}
          {discard && (
            <div
              className="video-discard"
              role="alertdialog"
              aria-label={t("Unsaved edits", "未保存的修改")}
            >
              <div>
                <h3>{t("Keep your edits?", "要保留当前修改吗？")}</h3>
                <p>
                  {t(
                    "Save this project before closing, or discard the unsaved changes.",
                    "关闭前保存工程，或放弃本次未保存的修改。",
                  )}
                </p>
                <div>
                  <Button variant="ghost" onClick={() => setDiscard(false)}>
                    {t("Continue editing", "继续编辑")}
                  </Button>
                  <Button variant="outline" onClick={close}>
                    {t("Discard", "放弃修改")}
                  </Button>
                  <Button
                    disabled={busy || remote}
                    onClick={() =>
                      perform(async () => {
                        await save();
                        close();
                      })
                    }
                  >
                    {t("Save and close", "保存并关闭")}
                  </Button>
                </div>
              </div>
            </div>
          )}
    </>
  );
  if (board) return <section className="video-workspace video-workspace-embedded" aria-label={t("Video composition", "视频合成")}>{body}</section>;
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) dismiss(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="video-overlay" />
        <Dialog.Content className="video-workspace" aria-describedby={undefined}
          onEscapeKeyDown={(event) => { event.preventDefault(); dismiss(); }}
          onInteractOutside={(event) => event.preventDefault()}>
          {body}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function VideoExportReview({ projectId, job, close }: {
  projectId: string;
  job: VideoJob;
  close: () => void;
}) {
  const { t } = useI18n();
  const [platform, setPlatform] = useState("");
  const [report, setReport] = useState<ExportReview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef<AbortController | null>(null);
  useEffect(() => () => requestRef.current?.abort(), []);

  async function runReview() {
    const controller = new AbortController();
    requestRef.current = controller;
    setBusy(true);
    setError("");
    setReport(null);
    try {
      setReport(await api<ExportReview>(`/video-projects/${projectId}/renders/${job.id}/review`, {
        method: "POST",
        body: json({ platform: platform || null }),
        signal: controller.signal,
      }));
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const failed = report?.checks.filter((check) => check.status === "FAIL").length ?? 0;
  const warned = report?.checks.filter((check) => check.status === "WARN").length ?? 0;
  return (
    <section className="video-review" aria-label={t("Export review", "成片检查")} aria-busy={busy}>
      <header>
        <h3>{t("Export review", "成片检查")} · r{job.revision}</h3>
        <Button size="icon" variant="ghost" aria-label={t("Close export review", "关闭成片检查")}
          onClick={close} autoFocus><X size={16} /></Button>
      </header>
      <div className="video-review-options">
        <label className="video-field">
          {t("Delivery platform", "交付平台")}
          <select value={platform} disabled={busy} onChange={(event) => {
            setPlatform(event.target.value);
            setReport(null);
            setError("");
          }}>
            <option value="">{t("General review", "通用检查")}</option>
            <option value="youtube">YouTube</option>
            <option value="shorts">YouTube Shorts</option>
            <option value="tiktok">TikTok</option>
            <option value="reels">Instagram Reels</option>
          </select>
        </label>
        <Button onClick={() => void runReview()} disabled={busy}>
          {busy && <LoaderCircle size={14} className="spin" />}
          {busy ? t("Checking…", "检查中…") : t("Run review", "开始检查")}
        </Button>
      </div>
      {error && <p role="alert" className="video-notice error">{error}</p>}
      {report && (
        <>
          <p role="status">
            {t(`${report.checks.length} checks · ${failed} failed · ${warned} warnings`,
               `${report.checks.length} 项检查 · ${failed} 项未通过 · ${warned} 项提示`)}
          </p>
          <img src={videoMediaUrl(report.sheet_uri)} alt={t("Export contact sheet", "成片抽帧图")} />
          <ul className="video-review-results" aria-label={t("Check results", "检查结果")}>
            {report.checks.map((row) => (
              <li key={row.check}>
                <strong data-status={row.status}>{row.status}</strong>
                <span><b>{row.check}</b> · {row.value}
                  {row.status !== "PASS" && <small>{row.expected}{row.reason ? ` · ${row.reason}` : ""}</small>}
                </span>
              </li>
            ))}
          </ul>
          {report.notes.map((note, index) => <p className="muted" key={index}>{note}</p>)}
        </>
      )}
    </section>
  );
}

function SourceLibrary({ sources }: { sources: VideoSource[] }) {
  const { t } = useI18n();
  const { editor, setSelectedItem } = useTimelineContext();
  const bridge = useEditorActions();
  function canAppend(doc: VideoDocument, source: VideoSource) {
    const track = appendTrack(doc, source.kind);
    const start = Math.max(0, ...(track?.clips.map(clipEnd) ?? []));
    return clipCount(doc) < MAX_VIDEO_CLIPS
      && (!!track || doc.tracks.length < MAX_VIDEO_TRACKS)
      && start + (source.duration_us || 3_000_000) <= MAX_VIDEO_TIME_US;
  }
  function add(source: VideoSource) {
    const doc = bridge.current();
    if (!canAppend(doc, source)) return;
    const category = source.kind === "audio" ? "audio" : "picture";
    let track = appendTrack(doc, source.kind);
    if (!track) {
      track = {
        id: videoId(`t-${category}`),
        name: source.kind === "audio" ? t("Audio", "音频") : t("Video", "视频"),
        clips: [],
      };
      if (source.kind === "audio") doc.tracks.push(track);
      else doc.tracks.unshift(track);
    }
    const clip = newClip(source.kind, source);
    clip.start_us = Math.max(0, ...track.clips.map(clipEnd));
    track.clips.push(clip);
    bridge.replace(doc);
    const el = editor
      .getTimelineData()
      ?.tracks.flatMap((tr) => tr.getElements())
      .find((c) => c.getId() === clip.id);
    if (el) setSelectedItem(el);
  }
  return (
    <div className="video-sources">
      {sources.length ? (
        sources.map((source) => {
          const allowed = canAppend(bridge.document, source);
          return (
          <button
            key={source.id}
            className="video-source"
            onClick={() => add(source)}
            disabled={!allowed}
            title={allowed
              ? t("Add to timeline", "添加到时间线")
              : t("The timeline has reached its duration, layer or clip limit.", "已达到时间线时长、图层或片段上限。")}
          >
            <span className="video-source-thumbnail">
              {source.kind === "audio" ? (
                <Music size={24} />
              ) : (
                <img
                  loading="lazy"
                  src={`/api/video-projects/${bridge.projectId}/sources/${source.id}/frame`}
                  alt=""
                />
              )}
            </span>
            <span>
              <strong>{source.name}</strong>
              <small>
                {source.kind === "image"
                  ? `${source.width} × ${source.height}`
                  : `${seconds(source.duration_us)}s`}
              </small>
            </span>
            <Plus size={14} />
          </button>
          );
        })
      ) : (
        <p className="video-empty-library">
          {t("Import a file to start editing.", "导入素材，开始剪辑。")}
        </p>
      )}
    </div>
  );
}

// Context keeps sidebar additions and the inspector on the same document bridge.
import { createContext, useContext, type ReactNode } from "react";
type EditorActions = {
  document: VideoDocument;
  current: () => VideoDocument;
  replace: (doc: VideoDocument) => void;
  projectId: string;
};
const Actions = createContext<EditorActions | null>(null);
function useEditorActions() {
  return useContext(Actions)!;
}

function EditorBody({
  initial,
  sources,
  projectId,
  onChange,
  library,
  busy,
  boardLayers,
  addBoardImage,
}: {
  initial: VideoDocument;
  sources: VideoSource[];
  projectId: string;
  onChange: (doc: VideoDocument) => void;
  library: ReactNode;
  busy: boolean;
  boardLayers?: BoardVideoLayer[];
  addBoardImage: (layer: Extract<BoardVideoLayer, { kind: "image" }>) => Promise<void>;
}) {
  const { t } = useI18n();
  const {
    editor,
    changeLog,
    selectedItem,
    setSelectedItem,
    canUndo,
    canRedo,
    setVideoResolution,
  } = useTimelineContext();
  const current = useRef(initial);
  const [document, setDocument] = useState(initial);
  useEffect(() => {
    setVideoResolution({ width: document.width, height: document.height });
  }, [document.width, document.height, setVideoResolution]);
  const capture = useCallback(() => {
    const data = editor.getTimelineData();
    if (!data) return current.current;
    return fromTwick(
      {
        version: data.version,
        tracks: data.tracks.map((track) => track.serialize()),
      },
      current.current,
      sources,
    );
  }, [editor, sources]);
  useEffect(() => {
    if (!changeLog) return;
    const doc = capture();
    current.current = doc;
    setDocument(doc);
    onChange(doc);
  }, [changeLog, capture, onChange]);
  const replace = useCallback(
    (doc: VideoDocument) => {
      current.current = doc;
      setDocument(doc);
      onChange(doc);
      editor.loadProjectSnapshot(toTwick(doc, sources));
    },
    [editor, sources, onChange],
  );
  const selectedId =
    selectedItem instanceof TrackElement ? selectedItem.getId() : null;
  const selected = selectedId ? findClip(document, selectedId) : undefined;
  const owner = document.tracks.find((tr) =>
    tr.clips.some((c) => c.id === selected?.id),
  );
  const ordered = [...(owner?.clips || [])].sort(
    (a, b) => a.start_us - b.start_us,
  );
  const selectedIndex = ordered.findIndex((c) => c.id === selected?.id);
  const layerLimit = document.tracks.length >= MAX_VIDEO_TRACKS || clipCount(document) >= MAX_VIDEO_CLIPS;
  function addText(text: string) {
    const clip = {
      ...newClip("text"),
      text,
      x: 0.08,
      y: 0.75,
      width: 0.84,
      height: 0.2,
    };
    const next = withOverlay(capture(), clip, t("Text", "文字"));
    replace(next);
    const track = next.tracks.at(-1)!;
    const element = editor.getTrackById(track.id)?.getElements()[0];
    if (element) setSelectedItem(element);
  }
  function reorder(offset: number) {
    if (!selected || !owner) return;
    const neighbor = ordered[selectedIndex + offset];
    if (!neighbor) return;
    const doc = capture(),
      track = doc.tracks.find((tr) => tr.id === owner.id)!;
    const a = track.clips.find(
      (c) => c.id === (offset < 0 ? neighbor.id : selected.id),
    )!;
    const b = track.clips.find(
      (c) => c.id === (offset < 0 ? selected.id : neighbor.id),
    )!;
    const start = a.start_us,
      gap = Math.max(0, b.start_us - (a.start_us + a.duration_us));
    b.start_us = start;
    a.start_us = start + b.duration_us + gap;
    replace(doc);
  }
  function updateSelected(values: Partial<VideoClip>) {
    if (!selected) return;
    const next = capture();
    const clip = next.tracks
      .flatMap((tr) => tr.clips)
      .find((c) => c.id === selected.id);
    if (clip && Object.entries(values).some(([key, value]) => clip[key as keyof VideoClip] !== value)) {
      Object.assign(clip, values);
      replace(next);
    }
  }
  const source = sources.find((s) => s.id === selected?.source_id);
  const limits = selected
    ? clipTimingLimits(selected, ordered[selectedIndex - 1], ordered[selectedIndex + 1], source)
    : null;
  const ownerIndex = owner ? document.tracks.indexOf(owner) : -1;
  const isVisualTrack = (track: VideoDocument["tracks"][number]) =>
    track.clips.some((clip) => clip.kind !== "audio");
  let behind = -1;
  for (let index = ownerIndex - 1; index >= 0; index--) {
    if (!isVisualTrack(document.tracks[index])) continue;
    behind = index;
    break;
  }
  const ahead = document.tracks.findIndex((track, index) => index > ownerIndex && isVisualTrack(track));
  function moveLayer(destination: number) {
    const doc = capture();
    const index = doc.tracks.findIndex((track) => track.id === owner?.id);
    if (index < 0 || destination < 0 || destination === index) return;
    const [track] = doc.tracks.splice(index, 1);
    doc.tracks.splice(destination, 0, track);
    replace(doc);
  }
  const actions = useMemo(
    () => ({ document, current: capture, replace, projectId }),
    [document, capture, replace, projectId],
  );
  return (
    <Actions.Provider value={actions}>
      <div className={`video-body ${busy ? "is-busy" : ""}`}>
        <aside className="video-library">
          {boardLayers && (
            <section className="video-board-layers">
              <h3>{t("From this board", "当前白板素材")}</h3>
              {boardLayers.length ? boardLayers.map((layer) => (
                <Button
                  key={layer.id}
                  variant="ghost"
                  title={layer.label}
                  disabled={busy || layerLimit || (layer.kind === "text" && layer.text.length > 2000)}
                  onClick={() => layer.kind === "text" ? addText(layer.text) : void addBoardImage(layer)}
                >
                  {layer.kind === "text" ? <Type size={14} /> : <Plus size={14} />}
                  <span>{layer.label}</span>
                </Button>
              )) : <p>{t("Pin images or add text on the board to use them as overlays.", "在白板放入图片或文字，即可在这里叠加。")}</p>}
              {layerLimit && <p role="status">{t("Layer or clip limit reached.", "已达到图层或片段上限。")}</p>}
            </section>
          )}
          {library}
          <div className="video-library-label">{t("Overlays", "叠加内容")}</div>
          <Button
            variant="outline"
            disabled={busy || layerLimit}
            onClick={() => addText(t("Your title", "输入标题"))}
          >
            <Type size={15} />
            {t("Add text", "添加文字")}
          </Button>
        </aside>
        <div className="video-center">
          <div className="video-edit-controls">
            <span>{t("Preview & timeline", "预览与时间线")}</span>
            <span>
              {seconds(durationUs(document))}s · {document.width} ×{" "}
              {document.height}
            </span>
            <Button
              variant="ghost"
              disabled={!canUndo}
              onClick={() => editor.undo()}
            >
              {t("Undo", "撤销")}
            </Button>
            <Button
              variant="ghost"
              disabled={!canRedo}
              onClick={() => editor.redo()}
            >
              {t("Redo", "重做")}
            </Button>
          </div>
          <div className="video-twick">
            <NativeTimeline document={document} sources={sources}
              selected={busy ? undefined : selected} moveSelected={updateSelected} />
          </div>
          {!document.tracks.some((tr) => tr.clips.length) && (
            <div className="video-empty-stage">
              <Film size={28} />
              <strong>{t("Build your first cut", "开始第一段剪辑")}</strong>
              <span>
                {t(
                  "Add a source from the left to place it on the timeline.",
                  "点击左侧素材，将它添加到时间线。",
                )}
              </span>
            </div>
          )}
        </div>
        <aside className="video-inspector">
          <h3>
            {selected
              ? t("Clip properties", "片段属性")
              : t("Composition", "输出设置")}
          </h3>
          {selected && limits ? (
            <>
              <p className="video-selected-name">
                {source?.name || t("Text", "文字")}
              </p>
              <div className="video-reorder">
                <Button
                  variant="outline"
                  disabled={selectedIndex < 1}
                  onClick={() => reorder(-1)}
                >
                  {t("Move earlier", "向前移")}
                </Button>
                <Button
                  variant="outline"
                  disabled={
                    selectedIndex < 0 || selectedIndex >= ordered.length - 1
                  }
                  onClick={() => reorder(1)}
                >
                  {t("Move later", "向后移")}
                </Button>
              </div>
              <NumberField
                label={t("Timeline start (s)", "时间线起点（秒）")}
                value={selected.start_us / 1e6}
                min={limits.minStart / 1e6}
                max={Math.max(selected.start_us, limits.maxStart) / 1e6}
                onCommit={(v) =>
                  updateSelected({ start_us: Math.round(v * 1e6) })
                }
              />
              <NumberField
                label={t("Duration (s)", "时长（秒）")}
                value={selected.duration_us / 1e6}
                min={0.000001}
                max={Math.max(selected.duration_us, limits.maxDuration) / 1e6}
                onCommit={(v) =>
                  updateSelected({ duration_us: Math.round(v * 1e6) })
                }
              />
              {source && (selected.kind === "video" || selected.kind === "audio") ? (
                <>
                <NumberField
                  label={t("Source in-point (s)", "素材入点（秒）")}
                  value={selected.source_in_us / 1e6}
                  min={0}
                  max={Math.max(selected.source_in_us, limits.maxSourceIn) / 1e6}
                  onCommit={(v) =>
                    updateSelected({ source_in_us: Math.round(v * 1e6) })
                  }
                />
                <NumberField
                  label={t("Source out-point (s)", "素材出点（秒）")}
                  value={(selected.source_in_us + selected.duration_us) / 1e6}
                  min={(selected.source_in_us + 1) / 1e6}
                  max={(selected.source_in_us + Math.max(selected.duration_us, limits.maxDuration)) / 1e6}
                  onCommit={(v) => updateSelected({ duration_us: Math.round(v * 1e6) - selected.source_in_us })}
                />
                </>
              ) : null}
              <ClipTrimControls selected={selected} />
              {selected.kind === "text" ? (
                <>
                  <label className="video-field">
                    {t("Text", "文字")}
                    <ClipText key={selected.id} text={selected.text}
                      onCommit={(text) => updateSelected({ text })} />
                  </label>
                  <NumberField
                    label={t("Font size", "字号")}
                    value={selected.font_size}
                    min={8}
                    max={300}
                    step={1}
                    onCommit={(v) => updateSelected({ font_size: v })}
                  />
                  <label className="video-field">
                    {t("Text color", "文字颜色")}
                    <input
                      type="color"
                      value={selected.color}
                      onChange={(e) =>
                        updateSelected({ color: e.target.value })
                      }
                    />
                  </label>
                </>
              ) : null}
              {selected.kind !== "audio" && (
                <>
                  {selected.kind !== "text" && (
                    <label className="video-field">
                      {t("Frame fit", "画面适配")}
                      <select
                        value={selected.fit}
                        onChange={(e) =>
                          updateSelected({
                            fit: e.target.value as VideoClip["fit"],
                          })
                        }
                      >
                        <option value="contain">
                          {t("Fit inside", "完整显示")}
                        </option>
                        <option value="cover">
                          {t("Fill frame", "填满画幅")}
                        </option>
                      </select>
                    </label>
                  )}
                  <div className="video-field-pair">
                    <NumberField
                      label="X %"
                      value={selected.x * 100}
                      min={0}
                      max={(1 - selected.width) * 100}
                      step={1}
                      onCommit={(v) => updateSelected({ x: v / 100 })}
                    />
                    <NumberField
                      label="Y %"
                      value={selected.y * 100}
                      min={0}
                      max={(1 - selected.height) * 100}
                      step={1}
                      onCommit={(v) => updateSelected({ y: v / 100 })}
                    />
                  </div>
                  <label className="video-field">
                    {t("Opacity", "不透明度")} · {Math.round(selected.opacity * 100)}%
                    <input type="range" min={0} max={1} step={0.01} value={selected.opacity}
                      onChange={(e) => updateSelected({ opacity: Number(e.target.value) })} />
                  </label>
                  <div className="video-reorder">
                    <Button variant="outline" disabled={behind < 0} onClick={() => moveLayer(behind)}>
                      {t("Track backward", "轨道下移")}
                    </Button>
                    <Button variant="outline" disabled={ahead < 0} onClick={() => moveLayer(ahead)}>
                      {t("Track forward", "轨道上移")}
                    </Button>
                  </div>
                  <div className="video-field-pair">
                    <NumberField
                      label={t("Width %", "宽度 %")}
                      value={selected.width * 100}
                      min={1}
                      max={(1 - selected.x) * 100}
                      step={1}
                      onCommit={(v) => updateSelected({ width: v / 100 })}
                    />
                    <NumberField
                      label={t("Height %", "高度 %")}
                      value={selected.height * 100}
                      min={1}
                      max={(1 - selected.y) * 100}
                      step={1}
                      onCommit={(v) => updateSelected({ height: v / 100 })}
                    />
                  </div>
                </>
              )}
              {source?.has_audio && (
                <label className="video-field">
                  {t("Volume", "音量")} · {Math.round(selected.volume * 100)}%
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={selected.volume}
                    onChange={(e) =>
                      updateSelected({ volume: Number(e.target.value) })
                    }
                  />
                </label>
              )}
              <Button
                variant="outline"
                onClick={() => {
                  replace(removeClip(capture(), selected, false));
                  setSelectedItem(null);
                }}
              >
                {t("Delete clip", "删除片段")}
              </Button>
              {selectedIndex < ordered.length - 1 && (
                <Button variant="outline" onClick={() => {
                  replace(removeClip(capture(), selected, true));
                  setSelectedItem(null);
                }}>
                  {t("Delete and close gap on track", "删除并接合同轨")}
                </Button>
              )}
              <Button variant="ghost" onClick={() => setSelectedItem(null)}>
                {t("Output settings", "输出设置")}
              </Button>
            </>
          ) : (
            <>
              <label className="video-field">
                {t("Aspect ratio", "画幅")}
                <select
                  value={`${document.width}:${document.height}`}
                  onChange={(e) => {
                    const [width, height] = e.target.value
                      .split(":")
                      .map(Number);
                    replace({ ...capture(), width, height });
                  }}
                >
                  <option value="1280:720">16:9 · 1280 × 720</option>
                  <option value="720:1280">9:16 · 720 × 1280</option>
                  <option value="1080:1080">1:1 · 1080 × 1080</option>
                  <option value="1080:1350">4:5 · 1080 × 1350</option>
                  {!["1280:720", "720:1280", "1080:1080", "1080:1350"].includes(
                    `${document.width}:${document.height}`,
                  ) && (
                    <option value={`${document.width}:${document.height}`}>
                      {document.width} × {document.height}
                    </option>
                  )}
                </select>
              </label>
              <label className="video-field">
                {t("Frame rate", "帧率")}
                <select
                  value={`${document.fps_num}/${document.fps_den}`}
                  onChange={(e) => {
                    const [fps_num, fps_den] = e.target.value
                      .split("/")
                      .map(Number);
                    replace({ ...capture(), fps_num, fps_den });
                  }}
                >
                  <option value="24/1">24 fps</option>
                  <option value="25/1">25 fps</option>
                  <option value="30/1">30 fps</option>
                  <option value="30000/1001">29.97 fps</option>
                  <option value="60/1">60 fps</option>
                </select>
              </label>
              <label className="video-field">
                {t("Background", "背景色")}
                <input
                  type="color"
                  value={document.background}
                  onChange={(e) =>
                    replace({ ...capture(), background: e.target.value })
                  }
                />
              </label>
              <div className="video-tip">
                <Film size={18} />
                <p>
                  {t(
                    "Select a clip to trim it, set source in and out points, or adjust its picture and sound.",
                    "选择片段后可裁切、设置素材入点和出点，或调整画面和声音。",
                  )}
                </p>
                <small>
                  {t("Original files stay unchanged.", "原始素材始终保留。")}
                </small>
              </div>
            </>
          )}
        </aside>
      </div>
    </Actions.Provider>
  );
}

function ClipText({ text, onCommit }: { text: string; onCommit: (text: string) => void }) {
  const [draft, setDraft] = useState(text);
  useEffect(() => setDraft(text), [text]);
  return (
    <textarea value={draft} rows={3} maxLength={2000}
      onChange={(event) => {
        const next = event.target.value;
        setDraft(next);
        if (next.trim()) onCommit(next);
      }}
      onBlur={() => { if (!draft.trim()) setDraft(text); }}
    />
  );
}

function ClipTrimControls({ selected }: { selected: VideoClip }) {
  const { t } = useI18n();
  const { currentTime, seekTime, playerState, setCurrentTime, setSeekTime } = useLivePlayerContext();
  const actions = useEditorActions();
  const at = frameTimeUs(actions.document, currentTime);
  const within = at > selected.start_us && at < clipEnd(selected);
  const playing = playerState === PLAYER_STATE.PLAYING;
  function trim(side: "trim-start" | "trim-end") {
    const doc = actions.current();
    const next = cutClip(doc, selected, frameTimeUs(doc, currentTime), side);
    if (next !== doc) actions.replace(next);
  }
  return (
    <>
      <NumberField
        label={t("Playhead (s)", "播放头（秒）")}
        value={playing ? seekTime : currentTime}
        min={0}
        max={durationUs(actions.document) / 1e6}
        disabled={playing}
        onCommit={(time) => {
          setSeekTime(time);
          setCurrentTime(time);
        }}
      />
      <div className="video-reorder" role="group" aria-label={t("Trim at playhead", "按播放头裁切")}>
        <Button variant="outline" disabled={!within || playing} onClick={() => trim("trim-start")}>
          {t("Trim start here", "裁掉播放头前")}
        </Button>
        <Button variant="outline" disabled={!within || playing} onClick={() => trim("trim-end")}>
          {t("Trim end here", "裁掉播放头后")}
        </Button>
      </div>
    </>
  );
}

const fieldValue = (value: number) => String(Math.round(value * 1e6) / 1e6);

function NumberField({
  label,
  value,
  min,
  max,
  step = 0.01,
  disabled = false,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  onCommit: (v: number) => void;
}) {
  const [draft, setDraft] = useState(() => fieldValue(value));
  useEffect(() => setDraft(fieldValue(value)), [value]);
  function commit() {
    const number = Number(draft);
    if (!Number.isFinite(number) || draft.trim() === "" || draft === fieldValue(value)) {
      setDraft(fieldValue(value));
      return;
    }
    const accepted = Math.min(max ?? Infinity, Math.max(min, number));
    setDraft(fieldValue(accepted));
    onCommit(accepted);
  }
  return (
    <label className="video-field">
      {label}
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
        }}
      />
    </label>
  );
}

/** Browser-native media preview, driven by the same clock as Twick's timeline.
 * Keeping this adapter small avoids pulling its optional motion-graphics runtime
 * into a basic media edit and preserves our renderer's layout semantics. */
function NativeTimeline({
  document,
  sources,
  selected,
  moveSelected,
}: {
  document: VideoDocument;
  sources: VideoSource[];
  selected?: VideoClip;
  moveSelected: (position: Pick<VideoClip, "x" | "y">) => void;
}) {
  const {
    currentTime,
    seekTime,
    playerState,
    setPlayerState,
    setCurrentTime,
    setSeekTime,
  } = useLivePlayerContext();
  const { selectedItem, selectedIds, canUndo, canRedo } = useTimelineContext();
  const { deleteItem, handleUndo, handleRedo } = useTimelineControl();
  const actions = useEditorActions();
  function splitElement(element: TrackElement, at: number) {
    const doc = actions.current();
    const clip = findClip(doc, element.getId());
    if (!clip) return;
    const next = cutClip(doc, clip, frameTimeUs(doc, at), "split");
    if (next !== doc) actions.replace(next);
  }
  const [zoom, setZoom] = useState(1);
  const colors = useMemo(() => {
    const primary = getComputedStyle(globalThis.document.documentElement)
      .getPropertyValue("--primary")
      .trim();
    return Object.assign(
      { ...DEFAULT_ELEMENT_COLORS },
      Object.fromEntries(
        Object.keys(DEFAULT_ELEMENT_COLORS).map((key) => [key, primary]),
      ),
    );
  }, []);
  const total = durationUs(document) / 1e6;
  const timeRef = useRef(currentTime);
  timeRef.current = currentTime;
  const playing = playerState === PLAYER_STATE.PLAYING;
  const media = useRef(new Map<string, HTMLMediaElement>());
  const container = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 640, height: 360 });
  const [mediaError, setMediaError] = useState("");
  const drag = useRef<{ id: string; clientX: number; clientY: number } | null>(null);
  const [dragPosition, setDragPosition] = useState<{ id: string; x: number; y: number } | null>(null);
  const { t } = useI18n();
  const previewTime = currentTime >= total ? Math.max(0, total - 0.000001) : currentTime;
  const isVisible = (clip: VideoClip) =>
    previewTime >= clip.start_us / 1e6 && previewTime < (clip.start_us + clip.duration_us) / 1e6;
  const movable = selected && selected.kind !== "audio" && !playing && isVisible(selected) ? selected : null;
  const position = (clip: VideoClip) => dragPosition?.id === clip.id ? dragPosition : clip;
  useEffect(() => {
    const node = container.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) => {
      const ratio = document.width / document.height;
      const width = Math.min(
        entry.contentRect.width - 32,
        (entry.contentRect.height - 32) * ratio,
      );
      setSize({
        width: Math.max(1, width),
        height: Math.max(1, width / ratio),
      });
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [document.width, document.height]);
  useEffect(() => {
    setCurrentTime(seekTime);
  }, [seekTime, setCurrentTime]);
  useEffect(() => {
    if (!playing) return;
    const origin = performance.now(),
      start = timeRef.current >= total ? 0 : timeRef.current;
    let frame = 0;
    const tick = (now: number) => {
      const next = start + (now - origin) / 1000;
      if (next >= total) {
        setCurrentTime(total);
        setPlayerState(PLAYER_STATE.PAUSED);
        setSeekTime(total);
        return;
      }
      setCurrentTime(next);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, total, seekTime, setCurrentTime]);
  const clips = document.tracks.flatMap((track) => track.clips);
  useEffect(() => {
    for (const clip of clips) {
      const node = media.current.get(clip.id);
      if (!node || node.readyState < 1) continue;
      const active = isVisible(clip);
      const expected =
        clip.source_in_us / 1e6 +
        Math.max(0, previewTime - clip.start_us / 1e6);
      node.volume = Math.min(1, clip.volume);
      if (active) {
        if (Math.abs(node.currentTime - expected) > (playing ? 0.15 : 0.025))
          node.currentTime = expected;
        if (playing && node.paused)
          void node
            .play()
            .catch(() =>
              setMediaError(
                t(
                  "Playback could not start. Check this media file.",
                  "无法开始播放，请检查素材文件。",
                ),
              ),
            );
        else if (!playing && !node.paused) node.pause();
      } else if (!node.paused) node.pause();
    }
  }, [previewTime, playing, clips, t]);
  const seek = (time: number) => {
    setCurrentTime(time);
    setSeekTime(time);
  };
  return (
    <div className="video-native-editor">
      <div className="video-native-preview" ref={container}>
        <div
          className="video-preview-frame"
          style={{
            width: size.width,
            height: size.height,
            background: document.background,
          }}
        >
          {clips.map((clip) => {
            const source = sources.find((s) => s.id === clip.source_id);
            const active = isVisible(clip);
            const { x, y } = position(clip);
            const style = {
              position: "absolute" as const,
              left: `${x * 100}%`,
              top: `${y * 100}%`,
              width: `${clip.width * 100}%`,
              height: `${clip.height * 100}%`,
              opacity: active ? clip.opacity : 0,
              pointerEvents: "none" as const,
              objectFit: clip.fit,
            };
            if (clip.kind === "text")
              return (
                <div
                  key={clip.id}
                  style={{
                    ...style,
                    color: clip.color,
                    fontSize: (clip.font_size / document.width) * size.width,
                    lineHeight: 1.2,
                    whiteSpace: "pre-wrap",
                    overflowWrap: "anywhere",
                    overflow: "hidden",
                    fontFamily: 'Arial, "Microsoft YaHei", sans-serif',
                  }}
                >
                  {clip.text}
                </div>
              );
            if (!source) return null;
            if (clip.kind === "image")
              return (
                <img
                  key={clip.id}
                  src={videoMediaUrl(source.uri)}
                  style={style}
                  alt=""
                />
              );
            const props = {
              ref: (node: HTMLMediaElement | null) => {
                if (node) media.current.set(clip.id, node);
                else media.current.delete(clip.id);
              },
              src: videoMediaUrl(source.uri),
              preload: "auto" as const,
              onLoadedMetadata: (e: React.SyntheticEvent<HTMLMediaElement>) => {
                e.currentTarget.currentTime = clip.source_in_us / 1e6;
              },
              onError: () =>
                setMediaError(
                  t("A media file could not load.", "有素材无法加载。"),
                ),
            };
            return clip.kind === "audio" ? (
              <audio key={clip.id} {...props} />
            ) : (
              <video key={clip.id} {...props} style={style} playsInline />
            );
          })}
          {movable && (
            <div className="video-layer-bounds" aria-hidden="true"
              style={{
                left: `${position(movable).x * 100}%`,
                top: `${position(movable).y * 100}%`,
                width: `${movable.width * 100}%`,
                height: `${movable.height * 100}%`,
              }}
              onPointerDown={(event) => {
                if (event.button !== 0) return;
                event.currentTarget.setPointerCapture(event.pointerId);
                drag.current = { id: movable.id, clientX: event.clientX, clientY: event.clientY };
              }}
              onPointerMove={(event) => {
                const origin = drag.current;
                if (origin?.id !== movable.id) return;
                setDragPosition({
                  id: movable.id,
                  x: Math.max(0, Math.min(1 - movable.width, movable.x + (event.clientX - origin.clientX) / size.width)),
                  y: Math.max(0, Math.min(1 - movable.height, movable.y + (event.clientY - origin.clientY) / size.height)),
                });
              }}
              onPointerUp={() => {
                if (drag.current?.id === movable.id && dragPosition?.id === movable.id) {
                  moveSelected({ x: dragPosition.x, y: dragPosition.y });
                }
              }}
              onLostPointerCapture={() => {
                drag.current = null;
                setDragPosition(null);
              }}
            />
          )}
        </div>
        {mediaError && (
          <div className="video-preview-error" role="alert">
            {mediaError}
          </div>
        )}
      </div>
      <div className="video-native-timeline">
        <div className="twick-editor-timeline-controls">
          <PlayerControls
            selectedItem={selectedItem}
            selectedIds={selectedIds}
            duration={total}
            currentTime={currentTime}
            playerState={playerState}
            canUndo={canUndo}
            canRedo={canRedo}
            togglePlayback={() =>
              setPlayerState(
                playing ? PLAYER_STATE.PAUSED : PLAYER_STATE.PLAYING,
              )
            }
            onUndo={handleUndo}
            onRedo={handleRedo}
            onDelete={() => deleteItem()}
            onSplit={splitElement}
            zoomLevel={zoom}
            setZoomLevel={setZoom}
            fps={document.fps_num / document.fps_den}
            onSeek={seek}
          />
        </div>
        <TimelineManager trackZoom={zoom} elementColors={colors} />
      </div>
    </div>
  );
}
