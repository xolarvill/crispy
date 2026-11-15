/**
 * Translation between the server document (`BoardScene`) and the tldraw store.
 *
 * The document is canonical. This module is deliberately mechanical: it only
 * moves values, never invents meaning, so the agent-facing digest compiled on
 * the server always matches what the operator sees on the canvas.
 */
import {
  b64Vecs,
  createShapeId,
  getArrowBindings,
  getPointsFromDrawSegments,
  renderPlaintextFromRichText,
  toRichText,
  type Editor,
  type TLArrowShape,
  type TLDrawShape,
  type TLShape,
  type TLShapeId,
} from "tldraw";
import {
  ASSET_SHAPE,
  REGION_SHAPE,
  STAGE_SHAPE,
  boardIdOf,
  boardKindOf,
  metaOf,
  type BoardMeta,
} from "./board-shapes";
import type {
  BoardAssetItem,
  BoardItem,
  BoardLink,
  BoardNoteItem,
  BoardRegionItem,
  BoardScene,
  BoardSketchItem,
  BoardTextItem,
} from "./boards";
import { newBoardId } from "./boards";

export type ReadResult = {
  scene: BoardScene;
  warnings: string[];
  /** Shapes that received a freshly generated document id, for write-back. */
  assignments: [TLShapeId, string, "boardId" | "linkId"][];
};

const COLOR_TO_HEX: Record<string, string> = {
  black: "#1d1d1d",
  grey: "#868e96",
  "light-violet": "#e599f7",
  violet: "#ae3ec9",
  blue: "#4263eb",
  "light-blue": "#4dabf7",
  yellow: "#ffc078",
  orange: "#f76707",
  green: "#099268",
  "light-green": "#40c057",
  red: "#e03131",
  "light-red": "#ff8787",
  white: "#ffffff",
};

const HEX_TO_COLOR = Object.entries(COLOR_TO_HEX) as [string, string][];

const SIZE_TO_WIDTH: Record<string, number> = {
  s: 2,
  m: 3.5,
  l: 6,
  xl: 10,
};

const WIDTH_TO_SIZE: [number, string][] = [
  [10, "xl"],
  [6, "l"],
  [3.5, "m"],
  [2, "s"],
];

const SIZE_TO_FONT: Record<string, number> = {
  s: 14,
  m: 18,
  l: 24,
  xl: 36,
};

function fontToSize(fontSize: number): string {
  if (fontSize <= 14) return "s";
  if (fontSize <= 18) return "m";
  if (fontSize <= 24) return "l";
  return "xl";
}

export function colorHex(color: string) {
  return COLOR_TO_HEX[color] ?? "#1d1d1d";
}

export function colorName(hex: string) {
  let best = "black";
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const [name, value] of HEX_TO_COLOR) {
    const distance = colorDistance(hex, value);
    if (distance < bestDistance) {
      best = name;
      bestDistance = distance;
    }
  }
  return best;
}

function colorDistance(a: string, b: string) {
  const parse = (value: string) => {
    const match = /^#?([0-9a-f]{6})$/i.exec(value.trim());
    if (!match) return [0, 0, 0];
    const int = Number.parseInt(match[1], 16);
    return [(int >> 16) & 255, (int >> 8) & 255, int & 255];
  };
  const [ar, ag, ab] = parse(a);
  const [br, bg, bb] = parse(b);
  return (ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2;
}

function widthToSize(width: number) {
  for (const [max, size] of WIDTH_TO_SIZE) {
    if (width >= max - 0.5) return size;
  }
  return "s";
}

export function newNodeId(prefix: string) {
  return newBoardId(prefix);
}

type RichText = Parameters<typeof renderPlaintextFromRichText>[1];

function withMeta(boardId: string, kind: BoardMeta["boardKind"]): Partial<BoardMeta> {
  return { boardId, boardKind: kind };
}

/** Create every shape for a scene. Returns board node id → shape id. */
export function loadScene(editor: Editor, scene: BoardScene) {
  const shapeByNode = new Map<string, TLShapeId>();

  editor.run(
    () => {
      for (const item of scene.items) {
        if (item.type === "asset") {
          const shapeId = createShapeId();
          editor.createShape({
            id: shapeId,
            type: ASSET_SHAPE,
            x: item.x,
            y: item.y,
            meta: withMeta(item.id, "asset"),
            props: {
              w: item.w,
              h: item.h,
              uri: item.uri,
              assetId: item.assetId,
              mark: item.mark,
              role: item.role,
              caption: item.caption,
              mediaKind: /\.(mp4|mov|m4v|webm)(\?|$)/i.test(item.uri)
                ? "video"
                : "image",
            },
          });
          shapeByNode.set(item.id, shapeId);
        }
      }

      for (const item of scene.items) {
        if (item.type === "region") {
          const assetShapeId = shapeByNode.get(item.assetId);
          if (!assetShapeId) continue;
          const assetShape = editor.getShape(assetShapeId);
          const width = (assetShape?.props as { w?: number })?.w ?? 1;
          const height = (assetShape?.props as { h?: number })?.h ?? 1;
          const shapeId = createShapeId();
          editor.createShape({
            id: shapeId,
            type: REGION_SHAPE,
            parentId: assetShapeId,
            x: item.x * width,
            y: item.y * height,
            meta: withMeta(item.id, "region"),
            props: {
              w: Math.max(8, item.w * width),
              h: Math.max(8, item.h * height),
              assetId: item.assetId,
              note: item.note,
            },
          });
          shapeByNode.set(item.id, shapeId);
        } else if (item.type === "note") {
          const shapeId = createShapeId();
          editor.createShape({
            id: shapeId,
            type: "note",
            x: item.x,
            y: item.y,
            meta: withMeta(item.id, "note"),
            props: { richText: toRichText(item.text) },
          });
          shapeByNode.set(item.id, shapeId);
        } else if (item.type === "text") {
          const shapeId = createShapeId();
          editor.createShape({
            id: shapeId,
            type: "text",
            x: item.x,
            y: item.y,
            meta: withMeta(item.id, "text"),
            props: {
              richText: toRichText(item.text),
              autoSize: true,
              w: 360,
              size: fontToSize(item.fontSize) as "s" | "m" | "l" | "xl",
            },
          });
          shapeByNode.set(item.id, shapeId);
        } else if (item.type === "sketch") {
          shapeByNode.set(item.id, createSketchShape(editor, item));
        } else if (item.type === "stage") {
          const shapeId = createShapeId();
          editor.createShape({
            id: shapeId,
            type: STAGE_SHAPE,
            x: item.x,
            y: item.y,
            meta: withMeta(item.id, "stage"),
            props: {
              w: item.w,
              h: item.h,
              stage: item.stage,
            },
          });
          shapeByNode.set(item.id, shapeId);
        }
        // Frames and any future node types are preserved by the caller until
        // the canvas learns to render them.
      }

      for (const link of scene.links) {
        const from = shapeByNode.get(link.from);
        const to = shapeByNode.get(link.to);
        if (!from || !to) continue;
        createLinkArrow(editor, link, from, to);
      }
    },
    { history: "ignore" },
  );

  const { x, y, zoom } = scene.viewport;
  editor.setCamera({ x, y, z: zoom });

  return shapeByNode;
}

function createSketchShape(editor: Editor, item: BoardSketchItem): TLShapeId {
  const points = item.points.map(([x, y]) => ({ x, y }));
  const originX = Math.min(...points.map((point) => point.x));
  const originY = Math.min(...points.map((point) => point.y));
  const local = points.map((point) => ({
    x: point.x - originX,
    y: point.y - originY,
  }));
  const path = b64Vecs.encodePoints2D(local);
  const shapeId = createShapeId();
  editor.createShape({
    id: shapeId,
    type: "draw",
    x: originX,
    y: originY,
    meta: withMeta(item.id, "sketch"),
    props: {
      color: colorName(item.color) as never,
      fill: "none",
      dash: "draw",
      size: widthToSize(item.width) as never,
      segments: [{ type: "free", path, dim: 2 }],
      isComplete: true,
      isClosed: false,
      isPen: false,
      scale: 1,
      scaleX: 1,
      scaleY: 1,
    },
  });
  return shapeId;
}

function createLinkArrow(
  editor: Editor,
  link: BoardLink,
  from: TLShapeId,
  to: TLShapeId,
) {
  const fromBounds = editor.getShapePageBounds(from);
  const toBounds = editor.getShapePageBounds(to);
  if (!fromBounds || !toBounds) return;
  const arrowId = createShapeId();
  editor.createShape({
    id: arrowId,
    type: "arrow",
    x: 0,
    y: 0,
    meta: {
      boardId: link.id,
      linkId: link.id,
      linkKind: link.kind,
      linkPort: link.port ?? "",
      linkDataType: link.dataType ?? null,
    } satisfies BoardMeta,
    props: {
      start: { x: fromBounds.maxX, y: fromBounds.center.y },
      end: { x: toBounds.minX, y: toBounds.center.y },
      richText: toRichText(link.label),
      color: (link.kind === "data" ? "blue" : "black") as never,
      dash: (link.kind === "data" ? "solid" : "dashed") as never,
      arrowheadEnd: "arrow",
    },
  });
  editor.createBindings([
    {
      type: "arrow",
      fromId: arrowId,
      toId: from,
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
      toId: to,
      props: {
        terminal: "end",
        normalizedAnchor: { x: 0.5, y: 0.5 },
        isExact: false,
        isPrecise: true,
      },
    },
  ]);
}

function uniqueId(
  seed: string | null,
  prefix: string,
  used: Set<string>,
  assign?: (id: string) => void,
) {
  const generated = !seed || used.has(seed);
  let id = seed && !used.has(seed) ? seed : newNodeId(prefix);
  while (used.has(id)) id = newNodeId(prefix);
  used.add(id);
  if (generated) assign?.(id);
  return id;
}

/**
 * Read the canvas back into a document. `unmanaged` items (frames, future node
 * types) are passed through untouched so a round-trip never loses data the
 * canvas cannot render yet.
 */
export function readScene(
  editor: Editor,
  options: {
    goal: string;
    unmanaged?: BoardItem[];
  },
): ReadResult {
  const warnings: string[] = [];
  const shapes = editor.getCurrentPageShapes();
  const used = new Set<string>();
  const assignments: ReadResult["assignments"] = [];
  const items: BoardItem[] = [];
  const nodeIdByShape = new Map<TLShapeId, string>();
  const idFor = (shape: TLShape, prefix: string) =>
    uniqueId(boardIdOf(shape), prefix, used, (id) =>
      assignments.push([shape.id, id, "boardId"]),
    );

  const assetWidth = new Map<TLShapeId, number>();
  const assetHeight = new Map<TLShapeId, number>();

  for (const shape of shapes) {
    if (shape.type === ASSET_SHAPE) {
      const props = shape.props as {
        w: number;
        h: number;
        uri: string;
        assetId: string;
        mark: number;
        role: BoardAssetItem["role"];
        caption: string;
      };
      const id = idFor(shape, "a");
      nodeIdByShape.set(shape.id, id);
      assetWidth.set(shape.id, props.w);
      assetHeight.set(shape.id, props.h);
      items.push({
        type: "asset",
        id,
        x: shape.x,
        y: shape.y,
        w: props.w,
        h: props.h,
        uri: props.uri,
        assetId: props.assetId || id,
        mark: props.mark,
        role: props.role,
        caption: props.caption,
      });
    }
  }

  for (const shape of shapes) {
    const kind = boardKindOf(shape);
    if (shape.type === "note") {
      const text = renderPlaintextFromRichText(
        editor,
        (shape.props as unknown as { richText: RichText }).richText,
      ).trim();
      if (!text) continue;
      const id = idFor(shape, "n");
      nodeIdByShape.set(shape.id, id);
      const bounds = editor.getShapePageBounds(shape);
      items.push({
        type: "note",
        id,
        x: shape.x,
        y: shape.y,
        w: bounds?.width ?? 240,
        h: bounds?.height ?? 120,
        text,
      } satisfies BoardNoteItem);
    } else if (shape.type === "text") {
      const text = renderPlaintextFromRichText(
        editor,
        (shape.props as unknown as { richText: RichText }).richText,
      ).trim();
      if (!text) continue;
      const id = idFor(shape, "t");
      nodeIdByShape.set(shape.id, id);
      const size = (shape.props as { size?: string }).size ?? "m";
      items.push({
        type: "text",
        id,
        x: shape.x,
        y: shape.y,
        text,
        fontSize: SIZE_TO_FONT[size] ?? 18,
      } satisfies BoardTextItem);
    } else if (shape.type === "draw") {
      const id = idFor(shape, "s");
      nodeIdByShape.set(shape.id, id);
      const props = shape.props as unknown as {
        segments: TLDrawShape["props"]["segments"];
        scaleX: number;
        scaleY: number;
        color: string;
        size: string;
      };
      const points = getPointsFromDrawSegments(
        props.segments,
        props.scaleX,
        props.scaleY,
      ).map((point) => [shape.x + point.x, shape.y + point.y] as [number, number]);
      if (points.length < 2) continue;
      const stride = Math.max(1, Math.ceil(points.length / 2000));
      const sampled = points.filter((_, index) => index % stride === 0);
      items.push({
        type: "sketch",
        id,
        x: shape.x,
        y: shape.y,
        points: sampled,
        color: colorHex(props.color),
        width: SIZE_TO_WIDTH[props.size] ?? 2,
      } satisfies BoardSketchItem);
    } else if (shape.type === REGION_SHAPE) {
      const props = shape.props as {
        w: number;
        h: number;
        assetId: string;
        note: string;
      };
      const parent = editor.getShape(shape.parentId);
      const parentBoardId = parent ? nodeIdByShape.get(parent.id) : null;
      const width = parent ? assetWidth.get(parent.id) : undefined;
      const height = parent ? assetHeight.get(parent.id) : undefined;
      if (!parentBoardId || !width || !height) {
        warnings.push(
          shape.id + ": region is not attached to a known asset",
        );
        continue;
      }
      const note = props.note.trim();
      if (!note) {
        warnings.push(
          shape.id + ": region has no note and was not saved",
        );
        continue;
      }
      const id = idFor(shape, "r");
      nodeIdByShape.set(shape.id, id);
      const xn = clamp01(shape.x / width);
      const yn = clamp01(shape.y / height);
      items.push({
        type: "region",
        id,
        x: xn,
        y: yn,
        w: clamp01(props.w / width, 0.01),
        h: clamp01(props.h / height, 0.01),
        assetId: parentBoardId,
        note,
      } satisfies BoardRegionItem);
    } else if (shape.type === STAGE_SHAPE) {
      const props = shape.props as { w: number; h: number; stage: string };
      const id = idFor(shape, "st");
      nodeIdByShape.set(shape.id, id);
      items.push({
        type: "stage",
        id,
        x: shape.x,
        y: shape.y,
        w: props.w,
        h: props.h,
        stage: props.stage,
        params: {},
      });
    }
    void kind;
  }

  for (const item of options.unmanaged ?? []) {
    if (!used.has(item.id)) {
      used.add(item.id);
      items.push(item);
    }
  }

  const links: BoardLink[] = [];
  for (const shape of shapes) {
    if (shape.type !== "arrow") continue;
    const bindings = getArrowBindings(editor, shape as TLArrowShape);
    const fromShape = bindings.start
      ? editor.getShape(bindings.start.toId)
      : undefined;
    const toShape = bindings.end ? editor.getShape(bindings.end.toId) : undefined;
    if (!fromShape || !toShape) {
      warnings.push("an arrow is not connected at both ends");
      continue;
    }
    const from = nodeIdByShape.get(fromShape.id);
    const to = nodeIdByShape.get(toShape.id);
    if (!from || !to) continue;
    const meta = metaOf(shape);
    const label = renderPlaintextFromRichText(
      editor,
      (shape.props as unknown as { richText: RichText }).richText,
    ).trim();
    links.push({
      id: uniqueId(meta.linkId ?? null, "l", used, (id) =>
        assignments.push([shape.id, id, "linkId"]),
      ),
      kind: meta.linkKind === "data" ? "data" : "annotation",
      from,
      to,
      label,
      port: meta.linkKind === "data" ? meta.linkPort ?? "in" : undefined,
      dataType: meta.linkKind === "data" ? meta.linkDataType ?? undefined : undefined,
    });
  }

  const camera = editor.getCamera();
  return {
    warnings,
    assignments,
    scene: {
      version: 1,
      goal: options.goal,
      items,
      links,
      viewport: { x: camera.x, y: camera.y, zoom: camera.z },
    },
  };
}

function clamp01(value: number, minimum = 0) {
  if (!Number.isFinite(value)) return minimum;
  return Math.min(1, Math.max(minimum, value));
}

/** Arrow shapes created by the operator are annotation links by definition. */
export function markArrowMeta(editor: Editor, shapeId: TLShapeId) {
  const shape = editor.getShape(shapeId);
  if (!shape || shape.type !== "arrow" || metaOf(shape).linkId) return;
  editor.updateShape({
    id: shapeId,
    type: "arrow",
    meta: {
      ...shape.meta,
      linkId: `l_${String(shapeId).replace(/^shape:/, "")}`,
      linkKind: "annotation",
    },
  });
}

export function makeArrowShapeId() {
  return createShapeId();
}
