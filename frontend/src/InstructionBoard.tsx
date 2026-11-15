/**
 * The instruction board workspace: a shared canvas where the operator marks
 * run assets, boxes regions, draws relations and writes literal direction for
 * the agents. Saves write the canonical document back to the server; the
 * server compiles it into the prompt digest shown in the side panel.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  createShapeId,
  Tldraw,
  useValue,
  type Editor,
  type TLShape,
  type TLShapeId,
} from "tldraw";
import "tldraw/tldraw.css";
import {
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Hand,
  LoaderCircle,
  Maximize2,
  MousePointer2,
  Pencil,
  Plus,
  Redo2,
  RefreshCw,
  SquareDashed,
  StickyNote,
  Trash2,
  Type as TypeIcon,
  Undo2,
  Video,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { Button } from "./components/ui/button";
import { useI18n } from "./i18n";
import type { RunMedia } from "./data";
import {
  circledMark,
  compileBoard,
  fetchBoard,
  fetchBoardAssets,
  isVideoUri,
  mediaUrl,
  newBoardId,
  saveBoard,
  type BoardItem,
  type BoardPayload,
  type BoardRole,
  type BoardScene,
} from "./boards";
import {
  ASSET_SHAPE,
  REGION_SHAPE,
  REGION_TOOL,
  STAGE_SHAPE,
  boardShapeUtils,
  boardTools,
} from "./board-shapes";
import { loadScene, readScene } from "./board-mapping";

export type BoardTarget = {
  runId: string;
  title?: string;
};

const MAX_MARK = 99;
const SNAPSHOT_BUDGET = 2_900_000;

const ROLES: { value: BoardRole; en: string; zh: string }[] = [
  { value: "source", en: "Source", zh: "主体素材" },
  { value: "reference", en: "Reference", zh: "参考" },
  { value: "style", en: "Style", zh: "风格" },
  { value: "output", en: "Output", zh: "输出目标" },
];

function emptyScene(): BoardScene {
  return {
    version: 1,
    goal: "",
    items: [],
    links: [],
    viewport: { x: 0, y: 0, zoom: 1 },
  };
}

function shortHash(value: string) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) | 0;
  }
  return (hash >>> 0).toString(36);
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("image failed"));
    image.src = src;
  });
}

function nextMark(editor: Editor) {
  const used = new Set(
    editor
      .getCurrentPageShapes()
      .filter((shape) => shape.type === ASSET_SHAPE)
      .map((shape) => Number((shape.props as { mark?: number }).mark ?? 0)),
  );
  for (let mark = 1; mark <= MAX_MARK; mark += 1) {
    if (!used.has(mark)) return mark;
  }
  return null;
}

async function exportSnapshot(editor: Editor) {
  const ids = [...editor.getCurrentPageShapeIds()];
  if (!ids.length) return "";
  const attempts = [
    { pixelRatio: 1.5, quality: 0.8 },
    { pixelRatio: 1, quality: 0.6 },
  ];
  for (const attempt of attempts) {
    try {
      const { url } = await editor.toImageDataUrl(ids, {
        format: "jpeg",
        background: true,
        ...attempt,
      });
      if (url.length <= SNAPSHOT_BUDGET) return url;
    } catch {
      /* A video frame or a cross-origin image can make rasterizing fail. */
    }
  }
  return "";
}

export function InstructionBoard({
  target,
  close,
  notify,
}: {
  target: BoardTarget;
  close: () => void;
  notify?: (message: string) => void;
}) {
  const { t } = useI18n();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [board, setBoard] = useState<BoardPayload | null>(null);
  const [title, setTitle] = useState(target.title ?? "");
  const [goal, setGoal] = useState("");
  const [dirty, setDirty] = useState(false);
  const [digest, setDigest] = useState("");
  const [digestOpen, setDigestOpen] = useState(false);
  const [assets, setAssets] = useState<RunMedia[]>([]);
  const [selected, setSelected] = useState<TLShape[]>([]);
  const [version, setVersion] = useState(0);
  const [focusedRegion, setFocusedRegion] = useState<TLShapeId | null>(null);

  const titleRef = useRef(title);
  const goalRef = useRef(goal);
  const dirtyRef = useRef(false);
  const busyRef = useRef(false);
  const internalRef = useRef(false);
  const expectedRevisionRef = useRef<number | null>(null);
  const unmanagedRef = useRef<BoardItem[]>([]);
  const loadedEditorRef = useRef<Editor | null>(null);
  const frameRef = useRef(0);
  const assetStepRef = useRef(0);
  const dirtyCheckRef = useRef(0);
  const savedSceneRef = useRef("");
  const savedTitleRef = useRef("");

  useEffect(() => {
    titleRef.current = title;
  }, [title]);
  useEffect(() => {
    goalRef.current = goal;
  }, [goal]);
  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  const scheduleRender = useCallback(() => {
    if (frameRef.current) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = 0;
      setVersion((value) => value + 1);
    });
  }, []);

  // Canvas bookkeeping (label measurement, binding updates) can touch the
  // store right after a save without changing what the board means. Compare
  // the document with the saved scene before showing "changes since".
  const scheduleDirtyCheck = useCallback(() => {
    if (dirtyCheckRef.current) window.clearTimeout(dirtyCheckRef.current);
    dirtyCheckRef.current = window.setTimeout(() => {
      if (!editor || !savedSceneRef.current) return;
      try {
        const { scene } = readScene(editor, {
          goal: goalRef.current,
          unmanaged: unmanagedRef.current,
        });
        const same =
          JSON.stringify(scene) === savedSceneRef.current &&
          titleRef.current.trim() === savedTitleRef.current;
        dirtyRef.current = !same;
        setDirty(!same);
      } catch {
        // Keep the change marked dirty if the document cannot be read.
      }
    }, 300);
  }, [editor]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    Promise.all([
      fetchBoard(target.runId),
      fetchBoardAssets(target.runId).catch(() => [] as RunMedia[]),
    ])
      .then(([payload, media]) => {
        if (cancelled) return;
        setBoard(payload);
        setAssets(media);
        if (payload) {
          setTitle(payload.title || target.title || "");
          setGoal(payload.scene.goal || "");
          setDigest(payload.compiled?.digest || "");
          expectedRevisionRef.current = payload.revision;
          unmanagedRef.current = payload.scene.items.filter(
            (item) => item.type === "frame",
          );
        } else {
          setTitle(target.title || "");
        }
      })
      .catch((loadError: Error) => {
        if (!cancelled) setError(loadError.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [target.runId, target.title]);

  // Load the document once per mounted editor. StrictMode remounts the
  // component but reuses this guard per editor instance.
  useEffect(() => {
    if (!editor || loading || error) return;
    if (loadedEditorRef.current === editor) return;
    loadedEditorRef.current = editor;

    const scene = board?.scene ?? emptyScene();
    loadScene(editor, scene);
    editor.selectNone();
    if (board) {
      savedSceneRef.current = JSON.stringify(
        readScene(editor, {
          goal: goalRef.current,
          unmanaged: unmanagedRef.current,
        }).scene,
      );
      savedTitleRef.current = titleRef.current.trim();
    }
    // The store listeners below are registered after the load, so tick the
    // sidebar once to reflect the shapes that were just created.
    setVersion((value) => value + 1);

    const offDocument = editor.store.listen(
      (entry) => {
        if (entry.source !== "user") return;
        if (!internalRef.current) {
          dirtyRef.current = true;
          setDirty(true);
          scheduleDirtyCheck();
        }
        scheduleRender();
      },
      { scope: "document" },
    );
    const offSession = editor.store.listen(
      () => {
        const shapes = editor
          .getSelectedShapeIds()
          .map((id) => editor.getShape(id))
          .filter((shape): shape is TLShape => Boolean(shape));
        setSelected(shapes);
      },
      { scope: "session", source: "all" },
    );
    return () => {
      offDocument();
      offSession();
    };
  }, [editor, loading, error, board, scheduleRender, scheduleDirtyCheck]);

  useEffect(() => {
    const onNotice = (event: Event) => {
      setNotice((event as CustomEvent<string>).detail || "");
    };
    const onFocusRegion = (event: Event) => {
      setFocusedRegion((event as CustomEvent<TLShapeId>).detail ?? null);
    };
    window.addEventListener("crispy-board-notice", onNotice);
    window.addEventListener("crispy-board-focus-region", onFocusRegion);
    return () => {
      window.removeEventListener("crispy-board-notice", onNotice);
      window.removeEventListener("crispy-board-focus-region", onFocusRegion);
    };
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
      if (dirtyCheckRef.current) window.clearTimeout(dirtyCheckRef.current);
    };
  }, []);

  const saveNow = useCallback(async () => {
    if (!editor || busyRef.current) return;
    // Commit an in-progress text or label edit first; otherwise its final
    // store update lands after the snapshot and marks the board dirty again.
    if (editor.getEditingShapeId()) {
      (document.activeElement as HTMLElement | null)?.blur();
      editor.setEditingShape(null);
    }
    const emptyRegions = editor
      .getCurrentPageShapes()
      .filter(
        (shape) =>
          shape.type === REGION_SHAPE &&
          !String((shape.props as { note?: string }).note ?? "").trim(),
      );
    if (emptyRegions.length) {
      editor.select(emptyRegions[0].id);
      fitShape(editor, emptyRegions[0]);
      setFocusedRegion(emptyRegions[0].id);
      setNotice(
        t(
          "Every marked area needs one sentence: what should change there?",
          "每个框选区域都要写一句话：这里要怎么改？",
        ),
      );
      return;
    }

    setBusy(true);
    setNotice("");
    try {
      const { scene, warnings, assignments } = readScene(editor, {
        goal: goalRef.current,
        unmanaged: unmanagedRef.current,
      });
      const marks = scene.items
        .filter((item) => item.type === "asset")
        .map((item) => item.mark);
      if (new Set(marks).size !== marks.length) {
        throw new Error(
          t(
            "Two assets share the same number. Give each asset its own number.",
            "有两个素材用了同一个编号，请给每个素材不同的编号。",
          ),
        );
      }
      const snapshot = await exportSnapshot(editor);
      const saved = await saveBoard(target.runId, {
        scene,
        title: titleRef.current.trim(),
        actor: "human",
        expected_revision: expectedRevisionRef.current,
        snapshot,
      });
      expectedRevisionRef.current = saved.revision;
      savedSceneRef.current = JSON.stringify(scene);
      savedTitleRef.current = titleRef.current.trim();
      setBoard(saved);
      setDigest(saved.compiled?.digest || "");
      dirtyRef.current = false;
      setDirty(false);

      if (assignments.length) {
        internalRef.current = true;
        editor.run(
          () => {
            for (const [shapeId, id, key] of assignments) {
              const shape = editor.getShape(shapeId);
              if (!shape) continue;
              const meta = shape.meta as { boardId?: string; linkId?: string };
              if (meta[key]) continue;
              editor.updateShape({
                id: shapeId,
                type: shape.type,
                meta: { ...shape.meta, [key]: id },
              });
            }
          },
          { history: "ignore" },
        );
        internalRef.current = false;
      }

      notify?.(
        t(
          `Instruction board saved · revision ${saved.revision}`,
          `指令白板已保存 · 第 ${saved.revision} 版`,
        ),
      );
      if (warnings.length) setNotice(warnings[0]);
      else setNotice(t("Saved. Agents will read this on the next stage.", "已保存，下一阶段会读到这份指令。"));
    } catch (saveError) {
      const message = (saveError as Error).message;
      const status = (saveError as Error & { status?: number }).status;
      setNotice(
        status === 409
          ? t(
              "Someone saved a newer revision. Close and reopen the board to get the latest.",
              "白板已被更新到更新的版本。请关闭后重新打开以获取最新版本。",
            )
          : message,
      );
    } finally {
      setBusy(false);
    }
  }, [editor, notify, t, target.runId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void saveNow();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [saveNow]);

  const closeBoard = useCallback(() => {
    if (
      dirtyRef.current &&
      !window.confirm(
        t(
          "Discard the unsaved changes on this board?",
          "放弃白板上未保存的修改？",
        ),
      )
    ) {
      return;
    }
    close();
  }, [close, t]);

  async function addAsset(media: RunMedia) {
    if (!editor) return;
    const existing = editor
      .getCurrentPageShapes()
      .find(
        (shape) =>
          shape.type === ASSET_SHAPE &&
          (shape.props as { uri?: string }).uri === media.uri,
      );
    if (existing) {
      editor.select(existing.id);
      fitShape(editor, existing);
      return;
    }
    const mark = nextMark(editor);
    if (mark === null) {
      setNotice(t("The board is full (99 numbered assets).", "白板素材已达上限（99 个编号）。"));
      return;
    }
    const video = isVideoUri(media.uri);
    let width = 360;
    let height = 202;
    if (!video) {
      try {
        const image = await loadImage(mediaUrl(media.uri));
        const scale = Math.min(
          1,
          380 / Math.max(image.naturalWidth, image.naturalHeight),
        );
        width = Math.max(80, Math.round(image.naturalWidth * scale));
        height = Math.max(80, Math.round(image.naturalHeight * scale));
      } catch {
        width = 320;
        height = 400;
      }
    }
    const viewport = editor.getViewportPageBounds();
    const step = assetStepRef.current++ % 6;
    const x = viewport.center.x - width / 2 + step * 32;
    const y = viewport.center.y - height / 2 + step * 28;
    const shapeId = createShapeId();
    editor.createShape({
      id: shapeId,
      type: ASSET_SHAPE,
      x,
      y,
      meta: { boardKind: "asset", boardId: newBoardId("a") },
      props: {
        w: width,
        h: height,
        uri: media.uri,
        assetId: `m_${shortHash(media.uri)}`,
        mark,
        role: "source",
        caption: "",
        mediaKind: video ? "video" : "image",
      },
    });
    editor.select(shapeId);
  }

  async function refreshDigest() {
    if (!editor) return;
    try {
      const { scene } = readScene(editor, {
        goal: goalRef.current,
        unmanaged: unmanagedRef.current,
      });
      const compiled = await compileBoard(target.runId, scene);
      setDigest(compiled.digest);
      setDigestOpen(true);
    } catch (compileError) {
      setNotice((compileError as Error).message);
    }
  }

  const shapes = editor?.getCurrentPageShapes() ?? [];
  // `version` ticks on document changes so shape lists stay current.
  void version;

  const assetShapes = shapes.filter((shape) => shape.type === ASSET_SHAPE);
  const notedShapes = shapes.filter(
    (shape) => shape.type === "note" || shape.type === "text",
  );
  const stageShapes = shapes.filter((shape) => shape.type === STAGE_SHAPE);
  const onBoardUris = new Set(
    assetShapes.map((shape) => String((shape.props as { uri?: string }).uri)),
  );
  const selectedAsset =
    selected.length === 1 && selected[0].type === ASSET_SHAPE ? selected[0] : null;
  const selectedRegion =
    selected.length === 1 && selected[0].type === REGION_SHAPE ? selected[0] : null;

  return (
    <div
      className="board-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={t("Instruction board", "指令白板")}
    >
      <div className="board-shell">
        <header className="board-header">
          <div className="board-header-main">
            <div className="board-header-title">
              <span className="board-eyebrow">
                {t("Instruction board", "指令白板")}
              </span>
              <input
                className="board-title-input"
                value={title}
                maxLength={160}
                placeholder={t("Untitled board", "未命名白板")}
                aria-label={t("Board title", "白板标题")}
                onChange={(event) => {
                  setTitle(event.target.value);
                  dirtyRef.current = true;
                  setDirty(true);
                }}
              />
            </div>
            <span className={`board-revision${dirty ? " is-dirty" : ""}`}>
              {board
                ? dirty
                  ? t(`Changes since r${board.revision}`, `r${board.revision} 之后有修改`)
                  : t(`Saved · r${board.revision}`, `已保存 · r${board.revision}`)
                : t("New board", "新白板")}
            </span>
          </div>
          <div className="board-header-actions">
            <Button
              variant="outline"
              disabled={busy || loading || Boolean(error)}
              onClick={() => void saveNow()}
            >
              {busy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
              {t("Save", "保存")}
            </Button>
            <button
              type="button"
              className="board-close"
              aria-label={t("Close the board", "关闭白板")}
              onClick={closeBoard}
            >
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="board-body">
          <aside className="board-sidebar">
            <section className="board-section">
              <label className="board-label" htmlFor="board-goal">
                {t("What should the agents do?", "要 agent 做什么？")}
              </label>
              <textarea
                id="board-goal"
                className="board-goal"
                value={goal}
                maxLength={2000}
                rows={3}
                placeholder={t(
                  "e.g. Merge the model from ① into the scene from ②, keep her pose and the shoe detail.",
                  "例如：把①里的模特放进②的场景，保留姿势和鞋子细节。",
                )}
                onChange={(event) => {
                  setGoal(event.target.value);
                  dirtyRef.current = true;
                  setDirty(true);
                }}
              />
            </section>

            <section className="board-section">
              <div className="board-section-head">
                <p>{t("Run assets", "任务素材")}</p>
                <span>{assets.length}</span>
              </div>
              {assets.length ? (
                <div className="board-asset-picker">
                  {assets.map((media) => {
                    const used = onBoardUris.has(media.uri);
                    return (
                      <button
                        key={`${media.uri}-${media.source}`}
                        type="button"
                        className={`board-asset-chip${used ? " is-used" : ""}`}
                        title={media.uri}
                        onClick={() => void addAsset(media)}
                      >
                        <span className="board-asset-chip-media">
                          {isVideoUri(media.uri) ? (
                            <Video size={16} />
                          ) : (
                            <img src={mediaUrl(media.uri)} alt="" loading="lazy" />
                          )}
                          {used ? (
                            <span className="board-asset-chip-check">
                              <Check size={11} />
                            </span>
                          ) : (
                            <span className="board-asset-chip-add">
                              <Plus size={11} />
                            </span>
                          )}
                        </span>
                        <span className="board-asset-chip-label">
                          {media.source === "generated"
                            ? t("Generated", "生成")
                            : t("Input", "素材")}
                        </span>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <p className="board-empty-note">
                  {t(
                    "This run has no media yet. Upload assets on the run first.",
                    "这个任务还没有素材，请先在任务里上传。",
                  )}
                </p>
              )}
            </section>

            <section className="board-section">
              <div className="board-section-head">
                <p>{t("On the board", "画板内容")}</p>
                <span>{assetShapes.length + notedShapes.length + stageShapes.length}</span>
              </div>
              <div className="board-outline">
                {assetShapes.map((shape) => {
                  const props = shape.props as {
                    mark: number;
                    caption: string;
                    uri: string;
                  };
                  return (
                    <button
                      key={shape.id}
                      type="button"
                      className="board-outline-row"
                      onClick={() => {
                        editor?.select(shape.id);
                        if (editor) fitShape(editor, shape);
                      }}
                    >
                      <span className="board-outline-mark">{circledMark(props.mark)}</span>
                      <span className="board-outline-text">
                        {props.caption || t("Untitled asset", "未命名素材")}
                      </span>
                    </button>
                  );
                })}
                {notedShapes.map((shape) => (
                  <button
                    key={shape.id}
                    type="button"
                    className="board-outline-row"
                    onClick={() => {
                      editor?.select(shape.id);
                      if (editor) fitShape(editor, shape);
                    }}
                  >
                    <span className="board-outline-icon">
                      {shape.type === "note" ? (
                        <StickyNote size={13} />
                      ) : (
                        <TypeIcon size={13} />
                      )}
                    </span>
                    <span className="board-outline-text">
                      {shape.type === "note" ? t("Note", "便签") : t("Text", "文字")}
                    </span>
                  </button>
                ))}
                {stageShapes.map((shape) => (
                  <button
                    key={shape.id}
                    type="button"
                    className="board-outline-row"
                    onClick={() => {
                      editor?.select(shape.id);
                      if (editor) fitShape(editor, shape);
                    }}
                  >
                    <span className="board-outline-icon">
                      <SquareDashed size={13} />
                    </span>
                    <span className="board-outline-text">
                      {(shape.props as { stage?: string }).stage}
                    </span>
                  </button>
                ))}
                {!assetShapes.length && !notedShapes.length && !stageShapes.length ? (
                  <p className="board-empty-note">
                    {t(
                      "Add an asset, then draw a box on it and write what to change.",
                      "先添加素材，然后在素材上画框并写明要改什么。",
                    )}
                  </p>
                ) : null}
              </div>
            </section>

            {selectedAsset ? (
              <AssetInspector
                key={selectedAsset.id}
                editor={editor!}
                shape={selectedAsset}
                shapes={shapes}
                onNotice={setNotice}
              />
            ) : selectedRegion ? (
              <RegionInspector
                key={selectedRegion.id}
                editor={editor!}
                shape={selectedRegion}
                focus={focusedRegion === selectedRegion.id}
              />
            ) : selected.length ? (
              <section className="board-section">
                <div className="board-section-head">
                  <p>{t("Selection", "选中")}</p>
                  <span>{selected.length}</span>
                </div>
                <p className="board-empty-note">
                  {selected.length === 1 &&
                  (selected[0].type === "note" || selected[0].type === "text")
                    ? t(
                        "Double-click on the canvas to edit the text.",
                        "在画布上双击即可编辑文字。",
                      )
                    : t(
                        "Double-click an arrow to type its label.",
                        "双击箭头可以直接写说明。",
                      )}
                </p>
                <Button
                  variant="outline"
                                    onClick={() => editor?.deleteShapes(selected.map((shape) => shape.id))}
                >
                  <Trash2 size={14} />
                  {t("Delete", "删除")}
                </Button>
              </section>
            ) : null}

            <section className="board-section board-digest">
              <button
                type="button"
                className="board-digest-toggle"
                onClick={() => {
                  setDigestOpen((open) => !open);
                  if (!digest) void refreshDigest();
                }}
              >
                {digestOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                {t("What the agent will read", "agent 会读到的内容")}
              </button>
              {digestOpen ? (
                digest ? (
                  <>
                    <pre className="board-digest-text">{digest}</pre>
                    <div className="board-digest-actions">
                      <button
                        type="button"
                        className="board-link-button"
                        onClick={() => {
                          void navigator.clipboard.writeText(digest);
                          setNotice(t("Copied", "已复制"));
                        }}
                      >
                        <Copy size={12} />
                        {t("Copy", "复制")}
                      </button>
                      <button
                        type="button"
                        className="board-link-button"
                        onClick={() => void refreshDigest()}
                      >
                        <RefreshCw size={12} />
                        {t("Refresh preview", "刷新预览")}
                      </button>
                    </div>
                  </>
                ) : (
                  <p className="board-empty-note">
                    {t(
                      "Save or refresh to see the compiled instructions.",
                      "保存或刷新后可以看到编译后的指令。",
                    )}
                  </p>
                )
              ) : null}
            </section>
          </aside>

          <div className="board-canvas">
            <Tldraw
              shapeUtils={boardShapeUtils}
              tools={boardTools}
              hideUi
              autoFocus
              licenseKey={import.meta.env.VITE_TLDRAW_LICENSE_KEY as string | undefined}
              onMount={(mounted: Editor) => {
                if (import.meta.env.DEV) {
                  (window as unknown as { __boardEditor?: Editor }).__boardEditor =
                    mounted;
                }
                setEditor(mounted);
              }}
            />
            {editor ? <BoardToolbar editor={editor} /> : null}
            {!shapes.length && !loading ? (
              <div className="board-canvas-hint" aria-hidden="true">
                <p>{t("Start by adding a run asset", "先添加一个任务素材")}</p>
                <span>
                  {t(
                    "Then use the region tool to mark an area and write what to change, draw arrows between assets, or drop a note.",
                    "然后用框选工具圈出要改的地方并写说明，或用箭头连接两个素材，也可以放便签。",
                  )}
                </span>
              </div>
            ) : null}
          </div>
        </div>

        {notice ? <div className="board-notice">{notice}</div> : null}
        {error ? (
          <div className="board-fatal">
            <p>{error}</p>
            <Button variant="outline" onClick={closeBoard}>
              {t("Close", "关闭")}
            </Button>
          </div>
        ) : loading ? (
          <div className="board-fatal board-loading">
            <LoaderCircle size={20} className="spin" />
            {t("Loading the board", "正在加载白板")}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function fitShape(editor: Editor, shape: TLShape) {
  const bounds = editor.getShapePageBounds(shape);
  if (!bounds) return;
  const padded = bounds.expandBy(80);
  editor.zoomToBounds(padded, { animation: { duration: 220 }, inset: 0 });
}

function AssetInspector({
  editor,
  shape,
  shapes,
  onNotice,
}: {
  editor: Editor;
  shape: TLShape;
  shapes: TLShape[];
  onNotice: (message: string) => void;
}) {
  const { t } = useI18n();
  const props = shape.props as {
    mark: number;
    role: BoardRole;
    caption: string;
  };
  const regions = shapes.filter(
    (candidate) =>
      candidate.type === REGION_SHAPE && candidate.parentId === shape.id,
  );

  function updateMark(raw: string) {
    const mark = Number(raw);
    if (!Number.isInteger(mark) || mark < 1 || mark > MAX_MARK) return;
    const clash = editor
      .getCurrentPageShapes()
      .some(
        (candidate) =>
          candidate.type === ASSET_SHAPE &&
          candidate.id !== shape.id &&
          Number((candidate.props as { mark?: number }).mark) === mark,
      );
    if (clash) {
      onNotice(t(`Number ${mark} is already used.`, `编号 ${mark} 已被占用。`));
      return;
    }
    editor.updateShape({
      id: shape.id,
      type: ASSET_SHAPE,
      props: { mark },
    });
  }

  return (
    <section className="board-section board-inspector">
      <div className="board-section-head">
        <p>{t("Selected asset", "选中的素材")}</p>
        <span>{circledMark(props.mark)}</span>
      </div>
      <div className="board-field-grid">
        <label>
          <span>{t("Number", "编号")}</span>
          <input
            type="number"
            min={1}
            max={MAX_MARK}
            value={props.mark}
            onChange={(event) => updateMark(event.target.value)}
          />
        </label>
        <label>
          <span>{t("Role", "用途")}</span>
          <select
            value={props.role}
            onChange={(event) =>
              editor.updateShape({
                id: shape.id,
                type: ASSET_SHAPE,
                props: { role: event.target.value as BoardRole },
              })
            }
          >
            {ROLES.map((role) => (
              <option key={role.value} value={role.value}>
                {t(role.en, role.zh)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="board-field">
        <span>{t("Caption the agent sees", "agent 看到的素材名")}</span>
        <input
          value={props.caption}
          maxLength={300}
          placeholder={t("e.g. the model's own photo", "例如：模特实拍原图")}
          onChange={(event) =>
            editor.updateShape({
              id: shape.id,
              type: ASSET_SHAPE,
              props: { caption: event.target.value },
            })
          }
        />
      </label>
      <div className="board-region-list">
        <div className="board-section-head">
          <p>{t("Marked areas", "框选区域")}</p>
          <span>{regions.length}</span>
        </div>
        {regions.length ? (
          regions.map((region) => {
            const note = String((region.props as { note?: string }).note ?? "");
            return (
              <div key={region.id} className="board-region-row">
                <textarea
                  value={note}
                  maxLength={500}
                  rows={2}
                  placeholder={t(
                    "What should change in this box?",
                    "这个框里要改什么？",
                  )}
                  onChange={(event) =>
                    editor.updateShape({
                      id: region.id,
                      type: REGION_SHAPE,
                      props: { note: event.target.value },
                    })
                  }
                />
                <div className="board-region-actions">
                  <button
                    type="button"
                    className="board-link-button"
                    onClick={() => {
                      editor.select(region.id);
                      fitShape(editor, region);
                    }}
                  >
                    <Maximize2 size={12} />
                    {t("Locate", "定位")}
                  </button>
                  <button
                    type="button"
                    className="board-link-button is-danger"
                    onClick={() => editor.deleteShapes([region.id])}
                  >
                    <Trash2 size={12} />
                    {t("Remove", "删除")}
                  </button>
                </div>
              </div>
            );
          })
        ) : (
          <p className="board-empty-note">
            {t(
              "Draw a box on this asset with the region tool to mark what changes.",
              "用框选工具在这个素材上画框，标出要修改的地方。",
            )}
          </p>
        )}
      </div>
    </section>
  );
}

function RegionInspector({
  editor,
  shape,
  focus,
}: {
  editor: Editor;
  shape: TLShape;
  focus: boolean;
}) {
  const { t } = useI18n();
  const note = String((shape.props as { note?: string }).note ?? "");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!focus) return;
    const timer = window.setTimeout(() => inputRef.current?.focus(), 80);
    return () => window.clearTimeout(timer);
  }, [focus]);

  return (
    <section className="board-section board-inspector">
      <div className="board-section-head">
        <p>{t("Marked area", "框选区域")}</p>
        <span>
          <SquareDashed size={13} />
        </span>
      </div>
      <label className="board-field">
        <span>{t("What should change here?", "这里要改什么？")}</span>
        <textarea
          ref={inputRef}
          value={note}
          maxLength={500}
          rows={3}
          placeholder={t(
            "e.g. remove the price tag, keep the stitching",
            "例如：去掉吊牌，保留走线",
          )}
          onChange={(event) =>
            editor.updateShape({
              id: shape.id,
              type: REGION_SHAPE,
              props: { note: event.target.value },
            })
          }
        />
      </label>
      <Button
        variant="outline"
                onClick={() => editor.deleteShapes([shape.id])}
      >
        <Trash2 size={14} />
        {t("Remove this box", "删除这个框")}
      </Button>
    </section>
  );
}

function BoardToolbar({ editor }: { editor: Editor }) {
  const { t } = useI18n();
  const tool = useValue("board tool", () => editor.getCurrentToolId(), [editor]);
  const canUndo = useValue("board undo", () => editor.getCanUndo(), [editor]);
  const canRedo = useValue("board redo", () => editor.getCanRedo(), [editor]);

  const tools: {
    id: string;
    label: [string, string];
    icon: typeof MousePointer2;
  }[] = [
    { id: "select", label: ["Select", "选择"], icon: MousePointer2 },
    { id: "hand", label: ["Pan", "平移"], icon: Hand },
    { id: "arrow", label: ["Arrow", "箭头"], icon: ArrowUpRight },
    { id: "draw", label: ["Draw", "画笔"], icon: Pencil },
    { id: "note", label: ["Note", "便签"], icon: StickyNote },
    { id: "text", label: ["Text", "文字"], icon: TypeIcon },
    { id: REGION_TOOL, label: ["Mark an area", "框选区域"], icon: SquareDashed },
  ];

  return (
    <div className="board-toolbar" role="toolbar" aria-label={t("Board tools", "白板工具")}>
      {tools.map((entry) => {
        const Icon = entry.icon;
        const active = tool === entry.id || (entry.id === REGION_TOOL && tool.startsWith(REGION_TOOL));
        return (
          <button
            key={entry.id}
            type="button"
            className={`board-tool${active ? " is-active" : ""}`}
            aria-pressed={active}
            title={t(entry.label[0], entry.label[1])}
            onClick={() => editor.setCurrentTool(entry.id)}
          >
            <Icon size={16} />
          </button>
        );
      })}
      <span className="board-tool-divider" />
      <button
        type="button"
        className="board-tool"
        title={t("Undo", "撤销")}
        disabled={!canUndo}
        onClick={() => editor.undo()}
      >
        <Undo2 size={16} />
      </button>
      <button
        type="button"
        className="board-tool"
        title={t("Redo", "重做")}
        disabled={!canRedo}
        onClick={() => editor.redo()}
      >
        <Redo2 size={16} />
      </button>
      <span className="board-tool-divider" />
      <button
        type="button"
        className="board-tool"
        title={t("Zoom out", "缩小")}
        onClick={() => editor.zoomOut()}
      >
        <ZoomOut size={16} />
      </button>
      <button
        type="button"
        className="board-tool"
        title={t("Zoom in", "放大")}
        onClick={() => editor.zoomIn()}
      >
        <ZoomIn size={16} />
      </button>
      <button
        type="button"
        className="board-tool"
        title={t("Fit to content", "适应内容")}
        onClick={() => editor.zoomToFit({ animation: { duration: 200 } })}
      >
        <Maximize2 size={16} />
      </button>
    </div>
  );
}
