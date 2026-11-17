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
  AlignHorizontalSpaceAround,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  FilePlus2,
  Hand,
  History,
  LoaderCircle,
  Maximize2,
  MousePointer2,
  Pencil,
  Plus,
  Redo2,
  RefreshCw,
  Save,
  Sparkles,
  SquareDashed,
  StickyNote,
  Trash2,
  Type as TypeIcon,
  Undo2,
  Video,
  Wrench,
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
  createBoardDoc,
  deleteBoardDoc,
  fetchBoardAssets,
  fetchBoardDoc,
  fetchBoardDocEvents,
  fetchBoardDocRevision,
  fetchBoardDocRevisions,
  fetchBoardTabs,
  isVideoUri,
  mediaUrl,
  newBoardId,
  renameBoardDoc,
  saveBoard,
  saveBoardDoc,
  type BoardDocRevisionItem,
  type BoardEvent,
  type BoardItem,
  type BoardPayload,
  type BoardRole,
  type BoardScene,
  type BoardTab,
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

/** Swap the canvas to another document; loadScene only ever adds shapes. */
function replaceScene(editor: Editor, scene: BoardScene) {
  const existing = [...editor.getCurrentPageShapeIds()];
  if (existing.length) editor.deleteShapes(existing);
  loadScene(editor, scene);
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

const ASSET_GAP = 40;

type AssetEntry = { shape: TLShape; bounds: NonNullable<ReturnType<Editor["getShapePageBounds"]>> };

function assetEntries(editor: Editor): AssetEntry[] {
  const entries: AssetEntry[] = [];
  for (const shape of editor.getCurrentPageShapes()) {
    if (shape.type !== ASSET_SHAPE) continue;
    const bounds = editor.getShapePageBounds(shape);
    if (bounds) entries.push({ shape, bounds });
  }
  return entries;
}

function unionOf(entries: AssetEntry[]) {
  const boxes = entries.map((entry) => entry.bounds);
  const x = Math.min(...boxes.map((box) => box.x));
  const y = Math.min(...boxes.map((box) => box.y));
  const maxX = Math.max(...boxes.map((box) => box.maxX));
  const maxY = Math.max(...boxes.map((box) => box.maxY));
  return { x, y, w: maxX - x, h: maxY - y };
}

/** New assets join a single horizontal row, so they never cover each other. */
function nextAssetPosition(editor: Editor, width: number, height: number) {
  const viewport = editor.getViewportPageBounds();
  const entries = assetEntries(editor);
  if (!entries.length) {
    return {
      x: viewport.center.x - width / 2,
      y: viewport.center.y - height / 2,
    };
  }
  return {
    x: Math.max(...entries.map((entry) => entry.bounds.maxX)) + ASSET_GAP,
    y: Math.min(...entries.map((entry) => entry.bounds.y)),
  };
}

/** Arrange every asset on one row, keeping their current left-to-right order. */
function tidyAssets(editor: Editor) {
  const entries = assetEntries(editor);
  if (entries.length < 2) return;
  entries.sort(
    (a, b) => a.bounds.x - b.bounds.x || a.bounds.y - b.bounds.y,
  );
  const startX = entries[0].bounds.x;
  const top = Math.min(...entries.map((entry) => entry.bounds.y));
  let cursor = startX;
  editor.run(() => {
    for (const { shape, bounds } of entries) {
      const dx = cursor - bounds.x;
      const dy = top - bounds.y;
      if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) {
        editor.updateShape({
          id: shape.id,
          type: shape.type,
          x: shape.x + dx,
          y: shape.y + dy,
        });
      }
      cursor += bounds.w + ASSET_GAP;
    }
  });
  const arranged = assetEntries(editor);
  if (arranged.length) {
    editor.zoomToBounds(unionOf(arranged), {
      inset: 100,
      animation: { duration: 220 },
    });
  }
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
  editVideo,
}: {
  target: BoardTarget;
  close: () => void;
  notify?: (message: string) => void;
  editVideo?: (uri: string) => void;
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
  const [trace, setTrace] = useState<BoardEvent[]>([]);
  const [traceError, setTraceError] = useState("");
  const [openAssets, setOpenAssets] = useState(true);
  const [openOutline, setOpenOutline] = useState(true);
  const [openTrace, setOpenTrace] = useState(true);
  const [assets, setAssets] = useState<RunMedia[]>([]);
  const [selected, setSelected] = useState<TLShape[]>([]);
  const [version, setVersion] = useState(0);
  const [focusedRegion, setFocusedRegion] = useState<TLShapeId | null>(null);
  const [tabs, setTabs] = useState<BoardTab[]>([]);
  const [activeBoardId, setActiveBoardId] = useState<string | null>(null);
  const [tabMenuOpen, setTabMenuOpen] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<BoardDocRevisionItem[]>([]);
  const [historyBusy, setHistoryBusy] = useState(false);

  const titleRef = useRef(title);
  const goalRef = useRef(goal);
  const dirtyRef = useRef(false);
  const busyRef = useRef(false);
  const internalRef = useRef(false);
  const expectedRevisionRef = useRef<number | null>(null);
  const unmanagedRef = useRef<BoardItem[]>([]);
  const loadedEditorRef = useRef<Editor | null>(null);
  const loadedBoardKeyRef = useRef<string | null>(null);
  const frameRef = useRef(0);
  const dirtyCheckRef = useRef(0);
  const savedSceneRef = useRef("");
  const savedTitleRef = useRef("");
  const lastRemoteRevisionRef = useRef<number | null>(null);

  useEffect(() => {
    titleRef.current = title;
  }, [title]);
  useEffect(() => {
    goalRef.current = goal;
  }, [goal]);
  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  /** Swap the active document into the workspace state. */
  const applyBoard = useCallback(
    (payload: BoardPayload | null) => {
      setBoard(payload);
      setTitle(payload ? payload.title || (target.title ?? "") : (target.title ?? ""));
      setGoal(payload?.scene.goal || "");
      setDigest(payload?.compiled?.digest || "");
      setHistoryOpen(false);
      setFocusedRegion(null);
      expectedRevisionRef.current = payload ? payload.revision : null;
      unmanagedRef.current =
        payload?.scene.items.filter((item) => item.type === "frame") ?? [];
      lastRemoteRevisionRef.current = null;
      dirtyRef.current = false;
      setDirty(false);
    },
    [target.title],
  );

  /** Apply a revision another writer saved while this board is open. */
  const applyRemoteBoard = useCallback(
    (payload: BoardPayload) => {
      if (!editor) return;
      internalRef.current = true;
      try {
        editor.run(() => replaceScene(editor, payload.scene), {
          history: "ignore",
        });
      } finally {
        internalRef.current = false;
      }
      // Undo must not step back into a scene the agent has already replaced.
      editor.clearHistory();
      editor.selectNone();
      loadedEditorRef.current = editor;
      loadedBoardKeyRef.current = payload.id;
      applyBoard(payload);
      savedSceneRef.current = JSON.stringify(
        readScene(editor, {
          goal: payload.scene.goal,
          unmanaged: payload.scene.items.filter((item) => item.type === "frame"),
        }).scene,
      );
      savedTitleRef.current = (payload.title || "").trim();
      setTabs((current) =>
        current.map((tab) =>
          tab.id === payload.id
            ? { ...tab, revision: payload.revision, updated_at: payload.updated_at ?? tab.updated_at }
            : tab,
        ),
      );
      setVersion((value) => value + 1);
      setNotice(
        t(
          `Agent updated this board · r${payload.revision}`,
          `Agent 已更新白板 · 第 ${payload.revision} 版`,
        ),
      );
    },
    [applyBoard, editor, t],
  );

  const confirmDiscard = useCallback(() => {
    if (!dirtyRef.current) return true;
    return window.confirm(
      t(
        "Discard the unsaved changes on this board?",
        "放弃白板上未保存的修改？",
      ),
    );
  }, [t]);

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

  const loadTrace = useCallback(async () => {
    if (!activeBoardId) {
      setTrace([]);
      setTraceError("");
      return;
    }
    try {
      setTrace(await fetchBoardDocEvents(target.runId, activeBoardId));
      setTraceError("");
    } catch (eventError) {
      setTraceError((eventError as Error).message);
    }
  }, [activeBoardId, target.runId]);

  // Poll while the board is open so a stage that reads it shows up live.
  useEffect(() => {
    void loadTrace();
    const timer = window.setInterval(() => void loadTrace(), 8000);
    return () => window.clearInterval(timer);
  }, [loadTrace]);

  // Poll the open document so a stage agent's canvas writes appear live.
  // Never replace unsaved local work: show a conflict notice instead.
  useEffect(() => {
    if (!editor || loading || error || !activeBoardId) return;
    let cancelled = false;
    const checkRemote = async () => {
      if (busyRef.current || editor.getEditingShapeId()) return;
      try {
        const payload = await fetchBoardDoc(target.runId, activeBoardId);
        if (cancelled || !payload) return;
        const current = expectedRevisionRef.current ?? 0;
        if (payload.revision <= current) return;
        // Unsaved local work wins: keep the canvas and warn instead of
        // replacing it, but still learn that a newer revision exists.
        if (dirtyRef.current || busyRef.current || editor.getEditingShapeId()) {
          if (lastRemoteRevisionRef.current !== payload.revision) {
            lastRemoteRevisionRef.current = payload.revision;
            setNotice(
              t(
                `Agent updated this board · r${payload.revision}. Save your edits first to avoid a conflict.`,
                `Agent 已更新白板 · 第 ${payload.revision} 版。先保存本地修改，避免冲突。`,
              ),
            );
          }
          return;
        }
        applyRemoteBoard(payload);
      } catch {
        // A poll failure is not worth interrupting the operator.
      }
    };
    const timer = window.setInterval(() => void checkRemote(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [
    activeBoardId,
    applyRemoteBoard,
    editor,
    error,
    loading,
    t,
    target.runId,
  ]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    Promise.all([
      fetchBoardTabs(target.runId),
      fetchBoardAssets(target.runId).catch(() => [] as RunMedia[]),
    ])
      .then(async ([tabList, media]) => {
        if (cancelled) return;
        setTabs(tabList);
        setAssets(media);
        const chosen =
          tabList.find((tab) => tab.is_current) ??
          [...tabList].reverse().find((tab) => tab.revision > 0) ??
          tabList[tabList.length - 1] ??
          null;
        if (!chosen) {
          applyBoard(null);
          return;
        }
        try {
          const payload = await fetchBoardDoc(target.runId, chosen.id);
          if (cancelled) return;
          setActiveBoardId(payload.id);
          applyBoard(payload);
        } catch (docError) {
          if (!cancelled) setError((docError as Error).message);
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
  }, [applyBoard, target.runId]);

  // Load the active document into the mounted editor. Switching tabs swaps the
  // scene and resets history so undo cannot reach into another board.
  useEffect(() => {
    if (!editor || loading || error) return;
    const boardKey = board?.id ?? "new";
    if (loadedEditorRef.current === editor && loadedBoardKeyRef.current === boardKey) {
      return;
    }
    const switching = loadedBoardKeyRef.current !== null;
    loadedEditorRef.current = editor;
    loadedBoardKeyRef.current = boardKey;

    const scene = board?.scene ?? emptyScene();
    // Programmatic load events must not read as operator edits.
    internalRef.current = true;
    try {
      if (switching) {
        replaceScene(editor, scene);
        // Undo must not reach back into another tab's document.
        editor.clearHistory();
      } else {
        loadScene(editor, scene);
      }
    } finally {
      internalRef.current = false;
    }
    editor.selectNone();
    // A fresh document is, by definition, in sync with what the server has.
    savedSceneRef.current = JSON.stringify(
      readScene(editor, {
        goal: goalRef.current,
        unmanaged: unmanagedRef.current,
      }).scene,
    );
    savedTitleRef.current = titleRef.current.trim();
    // The store listeners below are registered after the load, so tick the
    // sidebar once to reflect the shapes that were just created.
    setVersion((value) => value + 1);
  }, [editor, loading, error, board]);

  // Track operator edits for the dirty flag. This must live in its own effect:
  // a remote agent save refreshes `board`, which re-runs the loader above but
  // must not tear these listeners down with it.
  useEffect(() => {
    if (!editor || loading || error) return;
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
  }, [editor, loading, error, scheduleRender, scheduleDirtyCheck]);

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
      const payload = {
        scene,
        title: titleRef.current.trim(),
        actor: "human",
        expected_revision: expectedRevisionRef.current,
        snapshot,
      };
      const saved = activeBoardId
        ? await saveBoardDoc(target.runId, activeBoardId, payload)
        : await saveBoard(target.runId, payload);
      expectedRevisionRef.current = saved.revision;
      // The canvas already holds this document, so remember it as loaded and
      // keep the selection the operator had before saving.
      loadedEditorRef.current = editor;
      loadedBoardKeyRef.current = saved.id;
      setActiveBoardId(saved.id);
      savedSceneRef.current = JSON.stringify(scene);
      const savedTitle = saved.title || titleRef.current.trim();
      titleRef.current = savedTitle;
      savedTitleRef.current = savedTitle;
      setTitle(savedTitle);
      setBoard(saved);
      setDigest(saved.compiled?.digest || "");
      void fetchBoardDocEvents(target.runId, saved.id)
        .then(setTrace)
        .catch((traceError: Error) => setTraceError(traceError.message));
      void fetchBoardTabs(target.runId).then(setTabs).catch(() => undefined);
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
  }, [activeBoardId, editor, notify, t, target.runId]);

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

  const openTab = useCallback(
    async (tab: BoardTab) => {
      if (tab.id === activeBoardId || busyRef.current) return;
      if (!confirmDiscard()) return;
      setBusy(true);
      setTabMenuOpen(false);
      try {
        const payload = await fetchBoardDoc(target.runId, tab.id);
        setActiveBoardId(payload.id);
        applyBoard(payload);
      } catch (switchError) {
        setNotice((switchError as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [activeBoardId, applyBoard, confirmDiscard, target.runId],
  );

  const addTab = useCallback(
    async (kind: "duplicate" | "blank") => {
      if (busyRef.current || !confirmDiscard()) return;
      setBusy(true);
      setTabMenuOpen(false);
      try {
        const current = tabs.find((tab) => tab.id === activeBoardId);
        const base = current?.title || board?.title || "";
        const title =
          kind === "duplicate" && base
            ? t(`${base} copy`, `${base} 副本`)
            : "";
        const payload = await createBoardDoc(target.runId, {
          ...(kind === "duplicate" && activeBoardId
            ? { source_board_id: activeBoardId, title }
            : {}),
        });
        setTabs(await fetchBoardTabs(target.runId));
        setActiveBoardId(payload.id);
        applyBoard(payload);
        setNotice(
          kind === "duplicate" && activeBoardId
            ? t("Copied this board into a new tab.", "已复制到新的白板标签页。")
            : t("Opened a new board tab.", "已新建白板标签页。"),
        );
      } catch (tabError) {
        setNotice((tabError as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [activeBoardId, applyBoard, board?.title, confirmDiscard, t, tabs, target.runId],
  );

  const removeTab = useCallback(
    async (tab: BoardTab) => {
      if (busyRef.current) return;
      const question =
        tab.revision > 0
          ? t(
              "Delete this board tab and all of its saved versions?",
              "删除这个白板标签页和它的所有已保存版本？",
            )
          : t("Delete this empty board tab?", "删除这个空白白板标签页？");
      if (!window.confirm(question)) return;
      setBusy(true);
      try {
        await deleteBoardDoc(target.runId, tab.id);
        const tabList = await fetchBoardTabs(target.runId);
        setTabs(tabList);
        if (tab.id !== activeBoardId) return;
        const next =
          tabList.find((candidate) => candidate.is_current) ??
          [...tabList].reverse().find((candidate) => candidate.revision > 0) ??
          tabList[tabList.length - 1] ??
          null;
        if (next) {
          const payload = await fetchBoardDoc(target.runId, next.id);
          setActiveBoardId(payload.id);
          applyBoard(payload);
        } else {
          setActiveBoardId(null);
          applyBoard(null);
        }
      } catch (tabError) {
        setNotice((tabError as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [activeBoardId, applyBoard, t, target.runId],
  );

  function startRename(tab: BoardTab) {
    setTabMenuOpen(false);
    setHistoryOpen(false);
    setRenamingId(tab.id);
    setRenameValue(tab.title);
  }

  const commitRename = useCallback(async () => {
    const boardId = renamingId;
    if (!boardId) return;
    const nextTitle = renameValue.trim();
    setRenamingId(null);
    const previous = tabs.find((tab) => tab.id === boardId);
    if (!previous || previous.title === nextTitle) return;
    try {
      const saved = await renameBoardDoc(target.runId, boardId, nextTitle);
      setTabs((list) =>
        list.map((tab) =>
          tab.id === boardId ? { ...tab, title: saved.title } : tab,
        ),
      );
      if (boardId === activeBoardId) {
        titleRef.current = saved.title;
        savedTitleRef.current = saved.title;
        setTitle(saved.title);
        setBoard((current) =>
          current && current.id === boardId
            ? { ...current, title: saved.title }
            : current,
        );
        scheduleDirtyCheck();
      }
    } catch (renameError) {
      setNotice((renameError as Error).message);
    }
  }, [
    activeBoardId,
    renameValue,
    renamingId,
    scheduleDirtyCheck,
    tabs,
    target.runId,
  ]);

  const toggleHistory = useCallback(async () => {
    if (!activeBoardId) return;
    if (historyOpen) {
      setHistoryOpen(false);
      return;
    }
    setTabMenuOpen(false);
    setHistoryOpen(true);
    setHistoryBusy(true);
    try {
      setHistory(await fetchBoardDocRevisions(target.runId, activeBoardId));
    } catch (historyError) {
      setNotice((historyError as Error).message);
    } finally {
      setHistoryBusy(false);
    }
  }, [activeBoardId, historyOpen, target.runId]);

  const restoreRevision = useCallback(
    async (item: BoardDocRevisionItem) => {
      if (!editor || !activeBoardId) return;
      if (
        dirtyRef.current &&
        !window.confirm(
          t(
            "Replace the unsaved changes on this board with this version?",
            "用这个版本替换白板上未保存的修改？",
          ),
        )
      ) {
        return;
      }
      try {
        const revision = await fetchBoardDocRevision(
          target.runId,
          activeBoardId,
          item.revision,
        );
        replaceScene(editor, revision.scene);
        editor.selectNone();
        setGoal(revision.scene.goal || "");
        setHistoryOpen(false);
        dirtyRef.current = true;
        setDirty(true);
        setNotice(
          t(
            `Loaded r${item.revision} as an unsaved draft. Save to make it the current version.`,
            `已把第 ${item.revision} 版载入为未保存草稿，保存后才会成为当前版本。`,
          ),
        );
      } catch (revisionError) {
        setNotice((revisionError as Error).message);
      }
    },
    [activeBoardId, editor, t, target.runId],
  );

  // Close the tab menu and version popover when clicking elsewhere.
  useEffect(() => {
    if (!tabMenuOpen && !historyOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest(".board-tabbar")) return;
      setTabMenuOpen(false);
      setHistoryOpen(false);
    };
    window.addEventListener("pointerdown", onPointerDown);
    return () => window.removeEventListener("pointerdown", onPointerDown);
  }, [historyOpen, tabMenuOpen]);

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
    const { x, y } = nextAssetPosition(editor, width, height);
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
    // If the row now runs past the visible area, pull back so the new asset is
    // visible without the user having to hunt for it.
    const visible =
      x >= viewport.minX + 8 &&
      y >= viewport.minY + 8 &&
      x + width <= viewport.maxX - 8 &&
      y + height <= viewport.maxY - 8;
    if (!visible) {
      editor.zoomToBounds(unionOf(assetEntries(editor)), {
        inset: 100,
        animation: { duration: 220 },
      });
    }
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

  const traceView = trace.map((event) => {
    const data = event.payload || {},
      count = (value: unknown) => (typeof value === "number" ? value : 0),
      amount = (n: number, one: string, many: string) =>
        `${n} ${n === 1 ? one : many}`;
    const detail: string[] = [];
    if (count(data.assets))
      detail.push(
        t(
          amount(count(data.assets), "asset", "assets"),
          `${count(data.assets)} 个素材`,
        ),
      );
    if (count(data.regions))
      detail.push(
        t(
          amount(count(data.regions), "region", "regions"),
          `${count(data.regions)} 个框选`,
        ),
      );
    if (count(data.links))
      detail.push(
        t(
          amount(count(data.links), "link", "links"),
          `${count(data.links)} 条关系`,
        ),
      );
    if (!detail.length && count(data.items))
      detail.push(
        t(
          amount(count(data.items), "node", "nodes"),
          `${count(data.items)} 个节点`,
        ),
      );
    const delta = [
      count(data.added) ? `+${count(data.added)}` : "",
      count(data.changed) ? `~${count(data.changed)}` : "",
      count(data.removed) ? `-${count(data.removed)}` : "",
    ].filter(Boolean);
    const op =
      event.op === "read"
        ? "read_board"
        : event.op === "create"
          ? "create_board"
          : event.op === "save"
            ? "save_board"
            : event.op;
    const isTool = event.op.startsWith("tool:");
    const summary = typeof data.summary === "string" ? data.summary.trim() : "";
    const toolNames = Array.isArray(data.tools) ? data.tools : [];
    if (summary) {
      detail.unshift(summary.slice(0, 160));
    } else if (event.op === "agent_edit" && toolNames.length) {
      detail.unshift(amount(toolNames.length, "tool call", "tool calls"));
    }
    const images = Array.isArray(data.images) ? data.images : [];
    const image =
      isTool && typeof images[0] === "string" ? (images[0] as string) : null;
    return {
      event,
      op,
      isTool,
      image,
      detail: [...delta, ...detail].join(" · "),
      time: event.created_at
        ? new Date(event.created_at).toLocaleTimeString(undefined, {
            hour: "2-digit",
            minute: "2-digit",
          })
        : "",
      actor: event.actor_kind === "human" ? t("You", "你") : event.actor.replace(/_/g, " "),
    };
  });

  return (
    <div
      className="board-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={t("Instruction board", "指令白板")}
    >
      <div className="board-shell">
        <header className="board-header">
          <div className="board-tabbar">
            <div
              className="board-tabs"
              role="tablist"
              aria-label={t("Board tabs", "白板标签页")}
            >
              {tabs.map((tab) => {
                const active = tab.id === activeBoardId;
                const label = tab.title || t("Untitled board", "未命名白板");
                return (
                  <div
                    key={tab.id}
                    className={`board-tab${active ? " is-active" : ""}`}
                  >
                    {renamingId === tab.id ? (
                      <input
                        className="board-tab-rename"
                        autoFocus
                        value={renameValue}
                        maxLength={160}
                        aria-label={t("Board title", "白板标题")}
                        onChange={(event) => setRenameValue(event.target.value)}
                        onBlur={() => void commitRename()}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") {
                            event.preventDefault();
                            void commitRename();
                          }
                          if (event.key === "Escape") setRenamingId(null);
                        }}
                      />
                    ) : (
                      <button
                        type="button"
                        role="tab"
                        aria-selected={active}
                        className="board-tab-label"
                        title={
                          tab.is_current
                            ? t(
                                `${label} — agents read this board`,
                                `${label} — agent 读取这块白板`,
                              )
                            : t(
                                `${label} — double-click to rename`,
                                `${label} — 双击可重命名`,
                              )
                        }
                        onClick={() => void openTab(tab)}
                        onDoubleClick={() => startRename(tab)}
                      >
                        <span className="board-tab-title">{label}</span>
                        {active && dirty ? (
                          <span className="board-tab-dirty" aria-hidden="true" />
                        ) : null}
                      </button>
                    )}
                    <button
                      type="button"
                      className="board-tab-close"
                      aria-label={t(`Close ${label}`, `关闭 ${label}`)}
                      title={t("Close tab", "关闭标签页")}
                      onClick={() => void removeTab(tab)}
                    >
                      <X size={12} />
                    </button>
                  </div>
                );
              })}
            </div>
            <div className="board-tabbar-actions">
              <div className="board-tab-anchor">
                <button
                  type="button"
                  className="board-tab-new"
                  aria-label={t("New board tab", "新建白板标签页")}
                  title={t("New board tab", "新建白板标签页")}
                  aria-expanded={tabMenuOpen}
                  onClick={() => setTabMenuOpen((open) => !open)}
                >
                  <Plus size={14} />
                </button>
                {tabMenuOpen ? (
                  <div className="board-tab-menu" role="menu">
                    <button
                      type="button"
                      role="menuitem"
                      disabled={!activeBoardId}
                      onClick={() => void addTab("duplicate")}
                    >
                      <Copy size={13} />
                      <span>{t("Duplicate this board", "复制这块白板")}</span>
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => void addTab("blank")}
                    >
                      <FilePlus2 size={13} />
                      <span>{t("New blank board", "新建空白白板")}</span>
                    </button>
                  </div>
                ) : null}
              </div>
              <div className="board-tab-anchor">
                <button
                  type="button"
                  className={`board-tab-action${historyOpen ? " is-active" : ""}`}
                  disabled={!activeBoardId}
                  aria-label={t("Version history", "版本历史")}
                  title={t("Version history", "版本历史")}
                  aria-expanded={historyOpen}
                  onClick={() => void toggleHistory()}
                >
                  <History size={15} />
                </button>
                {historyOpen ? (
                  <div className="board-history-menu">
                    <div className="board-history-head">
                      <span>{t("Versions", "版本")}</span>
                      {board ? <small>r{board.revision}</small> : null}
                    </div>
                    {historyBusy ? (
                      <p className="board-history-empty">
                        <LoaderCircle size={13} className="spin" />
                        {t("Loading versions", "正在加载版本")}
                      </p>
                    ) : history.length ? (
                      <ol className="board-history-list">
                        {history.map((item) => (
                          <li key={item.revision}>
                            <button
                              type="button"
                              onClick={() => void restoreRevision(item)}
                            >
                              <span className="board-history-rev">
                                r{item.revision}
                              </span>
                              <small>
                                {item.created_at
                                  ? new Date(item.created_at).toLocaleString()
                                  : ""}
                              </small>
                            </button>
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <p className="board-history-empty">
                        {t("No saved versions yet.", "还没有已保存的版本。")}
                      </p>
                    )}
                  </div>
                ) : null}
              </div>
            </div>
          </div>
          <button
            type="button"
            className="board-close"
            aria-label={t("Close the board", "关闭白板")}
            onClick={closeBoard}
          >
            <X size={18} />
          </button>
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
                <button
                  type="button"
                  className="board-section-toggle"
                  aria-expanded={openAssets}
                  onClick={() => setOpenAssets((value) => !value)}
                >
                  {openAssets ? (
                    <ChevronDown size={13} />
                  ) : (
                    <ChevronRight size={13} />
                  )}
                  <span className="board-section-name">
                    {t("Run assets", "任务素材")}
                  </span>
                </button>
                <span>{assets.length}</span>
              </div>
              {openAssets ? (
                assets.length ? (
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
                )
              ) : null}
            </section>

            <section className="board-section">
              <div className="board-section-head">
                <button
                  type="button"
                  className="board-section-toggle"
                  aria-expanded={openOutline}
                  onClick={() => setOpenOutline((value) => !value)}
                >
                  {openOutline ? (
                    <ChevronDown size={13} />
                  ) : (
                    <ChevronRight size={13} />
                  )}
                  <span className="board-section-name">
                    {t("On the board", "画板内容")}
                  </span>
                </button>
                <span>{assetShapes.length + notedShapes.length + stageShapes.length}</span>
              </div>
              {openOutline ? (
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
              ) : null}
            </section>

            {selectedAsset ? (
              <>
              {selectedAsset.props.mediaKind === "video" && editVideo && <section className="board-section"><Button variant="outline" onClick={() => editVideo(selectedAsset.props.uri)}>{t("Edit video", "编辑视频")}</Button></section>}
              <AssetInspector
                key={selectedAsset.id}
                editor={editor!}
                shape={selectedAsset}
                shapes={shapes}
                onNotice={setNotice}
              />
              </>
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

            <section className="board-section board-trace">
              <div className="board-section-head">
                <button
                  type="button"
                  className="board-section-toggle"
                  aria-expanded={openTrace}
                  onClick={() => setOpenTrace((value) => !value)}
                >
                  {openTrace ? (
                    <ChevronDown size={13} />
                  ) : (
                    <ChevronRight size={13} />
                  )}
                  <span className="board-section-name">
                    {t("Agent trace", "Agent 记录")}
                  </span>
                </button>
                <span>{trace.length}</span>
              </div>
              {openTrace ? (
                traceView.length ? (
                <ol className="board-trace-list">
                  {traceView.map(({ event, op, detail, time, actor, image, isTool }) => (
                    <li
                      key={event.id}
                      className={`board-trace-row is-${event.actor_kind}${
                        isTool ? " is-tool" : ""
                      }`}
                    >
                      <span className="board-trace-icon" aria-hidden="true">
                        {isTool ? (
                          <Wrench size={12} />
                        ) : event.op === "read" ? (
                          <BookOpen size={12} />
                        ) : event.op === "create" ? (
                          <Sparkles size={12} />
                        ) : (
                          <Save size={12} />
                        )}
                      </span>
                      <span className="board-trace-body">
                        <span className="board-trace-line">
                          <strong>{actor}</strong>
                          <code className="board-trace-op">{op}</code>
                          <time
                            className="board-trace-time"
                            dateTime={event.created_at ?? undefined}
                            title={
                              event.created_at
                                ? new Date(event.created_at).toLocaleString()
                                : undefined
                            }
                          >
                            {time}
                          </time>
                        </span>
                        <small>
                          <span className="board-trace-rev">r{event.revision}</span>
                          {detail ? ` · ${detail}` : ""}
                        </small>
                        {image ? (
                          <img
                            className="board-trace-thumb"
                            src={mediaUrl(image)}
                            alt={op}
                            loading="lazy"
                          />
                        ) : null}
                      </span>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="board-empty-note">
                  {traceError ||
                    t(
                      "No agent activity yet. Stage agents log a read here each time they use this board.",
                      "还没有 agent 活动。阶段 agent 每次读取这块白板，这里都会留下记录。",
                    )}
                </p>
              )
              ) : null}
            </section>

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
                // Figma-like alignment: dragging an asset snaps its edges and
                // centres to nearby assets and shows guides. Holding the accel
                // key (Ctrl on Windows) still moves freely.
                mounted.user.updateUserPreferences({ isSnapMode: true });
                setEditor(mounted);
              }}
            />
            {editor ? <BoardToolbar editor={editor} /> : null}
            {editor ? (
              <QuickConnect editor={editor} onNotice={setNotice} />
            ) : null}
            {editor ? (
              <div className="board-save-cluster">
                <span
                  className={`board-save-state${dirty ? " is-dirty" : ""}`}
                  title={
                    board?.title
                      ? board.title
                      : t("This board has no saved version yet.", "这块白板还没有保存过。")
                  }
                >
                  {board && board.revision > 0
                    ? dirty
                      ? t(
                          `Changes since r${board.revision}`,
                          `r${board.revision} 之后有修改`,
                        )
                      : t(`Saved · r${board.revision}`, `已保存 · r${board.revision}`)
                    : t("Not saved yet", "尚未保存")}
                </span>
                <Button
                  variant="outline"
                  disabled={busy || loading || Boolean(error)}
                  onClick={() => void saveNow()}
                >
                  {busy ? (
                    <LoaderCircle size={14} className="spin" />
                  ) : (
                    <Check size={14} />
                  )}
                  {t("Save", "保存")}
                </Button>
              </div>
            ) : null}
            {!shapes.length && !loading ? (
              <div className="board-canvas-hint" aria-hidden="true">
                <p>{t("Start by adding a run asset", "先添加一个任务素材")}</p>
                <span>
                  {t(
                    "Then use the region tool to mark an area and write what to change. Hover an asset and drag a corner handle onto another one to link them, or drop a note.",
                    "然后用框选工具圈出要改的地方并写说明。把鼠标移到素材上，从边缘的小方块拖到另一个素材即可连线，也可以放便签。",
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

const QUICK_HANDLES = [
  { key: "top", fx: 0.5, fy: 0, dx: 0, dy: -1, rotate: -90 },
  { key: "right", fx: 1, fy: 0.5, dx: 1, dy: 0, rotate: 0 },
  { key: "bottom", fx: 0.5, fy: 1, dx: 0, dy: 1, rotate: 90 },
  { key: "left", fx: 0, fy: 0.5, dx: -1, dy: 0, rotate: 180 },
] as const;
/** Handles sit just outside the edge, like draw.io's link arrows. */
const QUICK_HANDLE_GAP = 11;
/** Pointer distance from the shape that reveals the handles. */
const QUICK_HOVER_MARGIN = 26;

type QuickDrag = {
  sourceId: TLShapeId;
  start: { x: number; y: number };
  startPage: { x: number; y: number };
};

/** Drawio-style link points: hover an asset and drag a handle onto another one
 * to create a bound arrow in a single undoable step. */
function QuickConnect({
  editor,
  onNotice,
}: {
  editor: Editor;
  onNotice?: (message: string) => void;
}) {
  const { t } = useI18n();
  const [hoveredAssetId, setHoveredAssetId] = useState<TLShapeId | null>(null);
  const [drag, setDrag] = useState<{
    sourceId: TLShapeId;
    start: { x: number; y: number };
    point: { x: number; y: number };
    targetId: TLShapeId | null;
    targetBounds: { x: number; y: number; w: number; h: number } | null;
  } | null>(null);
  const dragRef = useRef<QuickDrag | null>(null);
  const selectedIds = useValue(
    "board quick selection",
    () => editor.getSelectedShapeIds(),
    [editor],
  );
  const toolId = useValue(
    "board quick tool",
    () => editor.getCurrentToolId(),
    [editor],
  );
  const editing = useValue(
    "board quick editing",
    () => editor.getEditingShapeId(),
    [editor],
  );
  // Reading the camera re-renders the handles as the canvas pans or zooms.
  useValue("board quick camera", () => editor.getCamera(), [editor]);
  const dragging = drag !== null;

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      if (dragRef.current) return;
      // screenToPage takes client coordinates; pageToScreen/pageToViewport are
      // different spaces, so never mix them with the overlay's own offsets.
      const page = editor.screenToPage({
        x: event.clientX,
        y: event.clientY,
      });
      const shape = editor.getShapeAtPoint(page, {
        hitInside: true,
        // Margins are page units; keep the reveal distance constant on screen.
        margin: QUICK_HOVER_MARGIN / editor.getZoomLevel(),
        filter: (candidate) => candidate.type === ASSET_SHAPE,
      });
      setHoveredAssetId((current) =>
        current === (shape?.id ?? null) ? current : (shape?.id ?? null),
      );
    };
    window.addEventListener("pointermove", onMove);
    return () => window.removeEventListener("pointermove", onMove);
  }, [editor]);

  useEffect(() => {
    if (!dragRef.current) return;
    const container = editor.getContainer();
    // pageToViewport is relative to the canvas box, which is where the
    // overlay is positioned; pageToScreen would add the window offset again.
    const toViewport = (event: PointerEvent) => {
      const rect = container.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    };
    const toPage = (event: PointerEvent) =>
      editor.screenToPage({ x: event.clientX, y: event.clientY });
    const findTarget = (event: PointerEvent) => {
      const current = dragRef.current;
      if (!current) return undefined;
      return editor.getShapeAtPoint(toPage(event), {
        hitInside: true,
        margin: 14 / editor.getZoomLevel(),
        filter: (candidate) =>
          candidate.type === ASSET_SHAPE && candidate.id !== current.sourceId,
      });
    };
    const onMove = (event: PointerEvent) => {
      const point = toViewport(event);
      const target = findTarget(event);
      const bounds = target ? editor.getShapePageBounds(target) : undefined;
      const targetBounds = bounds
        ? (() => {
            const topLeft = editor.pageToViewport({
              x: bounds.minX,
              y: bounds.minY,
            });
            const bottomRight = editor.pageToViewport({
              x: bounds.maxX,
              y: bounds.maxY,
            });
            return {
              x: topLeft.x - 4,
              y: topLeft.y - 4,
              w: bottomRight.x - topLeft.x + 8,
              h: bottomRight.y - topLeft.y + 8,
            };
          })()
        : null;
      setDrag((current) =>
        current
          ? { ...current, point, targetId: target?.id ?? null, targetBounds }
          : current,
      );
    };
    const finish = () => {
      dragRef.current = null;
      setDrag(null);
    };
    const onUp = (event: PointerEvent) => {
      const current = dragRef.current;
      if (!current) return;
      const target = findTarget(event);
      if (target) {
        const drop = toPage(event);
        const arrowId = createShapeId();
        editor.run(() => {
          editor.createShape({
            id: arrowId,
            type: "arrow",
            x: current.startPage.x,
            y: current.startPage.y,
            props: {
              start: { x: 0, y: 0 },
              end: {
                x: drop.x - current.startPage.x,
                y: drop.y - current.startPage.y,
              },
            },
          });
          editor.createBindings([
            {
              type: "arrow",
              fromId: arrowId,
              toId: current.sourceId,
              props: {
                terminal: "start",
                normalizedAnchor: { x: 0.5, y: 0.5 },
                isExact: false,
                isPrecise: true,
              },
            },
            {
              type: "arrow",
              fromId: arrowId,
              toId: target.id,
              props: {
                terminal: "end",
                normalizedAnchor: { x: 0.5, y: 0.5 },
                isExact: false,
                isPrecise: true,
              },
            },
          ]);
          editor.select(arrowId);
        });
        onNotice?.(
          t(
            "Linked. Double-click the arrow to describe the relation.",
            "已连接。双击箭头可以补充说明。",
          ),
        );
      }
      finish();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") finish();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("keydown", onKey);
    };
  }, [dragging, editor, onNotice, t]);

  const sourceId =
    drag?.sourceId ??
    (toolId === "select" && !editing
      ? hoveredAssetId ?? (selectedIds.length === 1 ? selectedIds[0] : null)
      : null);
  const source = sourceId ? editor.getShape(sourceId) : undefined;
  if (!source || source.type !== ASSET_SHAPE) return null;
  const bounds = editor.getShapePageBounds(source);
  if (!bounds) return null;
  const handles = QUICK_HANDLES.map((handle) => {
    const viewport = editor.pageToViewport({
      x: bounds.x + bounds.w * handle.fx,
      y: bounds.y + bounds.h * handle.fy,
    });
    return {
      ...handle,
      x: viewport.x + handle.dx * QUICK_HANDLE_GAP,
      y: viewport.y + handle.dy * QUICK_HANDLE_GAP,
    };
  });

  function begin(
    event: React.PointerEvent<HTMLButtonElement>,
    shapeId: TLShapeId,
    start: { x: number; y: number },
  ) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    dragRef.current = {
      sourceId: shapeId,
      start,
      startPage: editor.screenToPage({
        x: event.clientX,
        y: event.clientY,
      }),
    };
    setDrag({
      sourceId: shapeId,
      start,
      point: start,
      targetId: null,
      targetBounds: null,
    });
  }

  return (
    <>
      <div className="board-quick-handles">
        {handles.map((handle) => (
          <button
            key={handle.key}
            type="button"
            className="board-quick-handle"
            style={{ left: handle.x, top: handle.y }}
            title={t("Drag to connect this asset", "拖动连接到另一个素材")}
            aria-label={t("Connect from this asset", "从这个素材开始连线")}
            onPointerDown={(event) => begin(event, source.id, handle)}
          >
            <ChevronRight
              size={10}
              style={{ transform: `rotate(${handle.rotate}deg)` }}
            />
          </button>
        ))}
      </div>
      {drag ? (
        <svg className="board-quick-link" aria-hidden="true">
          <defs>
            <marker
              id="board-quick-arrowhead"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" style={{ fill: "var(--primary)" }} />
            </marker>
          </defs>
          <line
            x1={drag.start.x}
            y1={drag.start.y}
            x2={drag.point.x}
            y2={drag.point.y}
            style={{ stroke: "var(--primary)" }}
            strokeWidth={1.8}
            strokeDasharray="5 4"
            markerEnd="url(#board-quick-arrowhead)"
          />
        </svg>
      ) : null}
      {drag?.targetBounds ? (
        <div
          className="board-quick-target"
          style={{
            left: drag.targetBounds.x,
            top: drag.targetBounds.y,
            width: drag.targetBounds.w,
            height: drag.targetBounds.h,
          }}
        />
      ) : null}
    </>
  );
}

function BoardToolbar({ editor }: { editor: Editor }) {
  const { t } = useI18n();
  const tool = useValue("board tool", () => editor.getCurrentToolId(), [editor]);
  const canUndo = useValue("board undo", () => editor.getCanUndo(), [editor]);
  const canRedo = useValue("board redo", () => editor.getCanRedo(), [editor]);
  const assetCount = useValue(
    "board assets",
    () =>
      editor
        .getCurrentPageShapes()
        .filter((shape) => shape.type === ASSET_SHAPE).length,
    [editor],
  );

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
      <button
        type="button"
        className="board-tool"
        title={t("Tidy assets into a row", "把素材排成一行")}
        disabled={assetCount < 2}
        onClick={() => tidyAssets(editor)}
      >
        <AlignHorizontalSpaceAround size={16} />
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
