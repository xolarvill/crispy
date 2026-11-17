import type { ProjectJSON } from "@twick/timeline";

export type VideoSource = {
  id: string;
  name: string;
  uri: string;
  sha256: string;
  kind: "video" | "image" | "audio";
  width: number;
  height: number;
  duration_us: number;
  has_audio: boolean;
};
export type VideoClip = {
  id: string;
  kind: VideoSource["kind"] | "text";
  source_id: string | null;
  start_us: number;
  duration_us: number;
  source_in_us: number;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  fit: "contain" | "cover";
  volume: number;
  opacity: number;
  font_size: number;
  color: string;
};
export type VideoDocument = {
  version: 1;
  width: number;
  height: number;
  fps_num: number;
  fps_den: number;
  background: string;
  tracks: { id: string; name: string; clips: VideoClip[] }[];
};
export type VideoProject = {
  id: string;
  run_id: string;
  title: string;
  revision: number;
  document: VideoDocument;
  sources: VideoSource[];
};
export type VideoJob = {
  id: string;
  revision: number;
  status: "queued" | "running" | "completed" | "cancelled" | "failed";
  progress: number;
  error: string;
  uri: string;
  artifact_id: string | null;
};
export type VideoTarget = { runId: string; uri?: string; projectId?: string };
export const videoMediaUrl = (uri: string) =>
  `/api/media?path=${encodeURIComponent(uri)}`;
const sdkMediaUrl = (uri: string) =>
  new URL(
    videoMediaUrl(uri),
    typeof location === "undefined" ? "http://localhost" : location.origin,
  ).href;
export const videoId = (prefix = "e") => `${prefix}-${crypto.randomUUID()}`;
export const durationUs = (doc: VideoDocument) =>
  Math.max(
    0,
    ...doc.tracks.flatMap((t) =>
      t.clips.map((c) => c.start_us + c.duration_us),
    ),
  );
const sdkId = (id: string, prefix: string) =>
  id.startsWith(`${prefix}-`) ? id : `${prefix}-crispy-${id}`;

export function newClip(
  kind: VideoClip["kind"],
  source?: VideoSource,
): VideoClip {
  return {
    id: videoId(),
    kind,
    source_id: source?.id ?? null,
    start_us: 0,
    duration_us: source?.duration_us || 3_000_000,
    source_in_us: 0,
    text: kind === "text" ? "Your title" : "",
    x: 0,
    y: 0,
    width: 1,
    height: 1,
    fit: "contain",
    volume: 1,
    opacity: 1,
    font_size: 36,
    color: "#ffffff",
  };
}

/** Twick is a view. This adapter is the only place its JSON crosses our contract. */
export function toTwick(
  doc: VideoDocument,
  sources: VideoSource[],
): ProjectJSON {
  return {
    version: 1,
    tracks: doc.tracks.map((track) => ({
      id: sdkId(track.id, "t"),
      name: track.name,
      type: "element",
      props: { crispyTrackId: track.id },
      elements: track.clips.map((clip) => {
        const source = sources.find((s) => s.id === clip.source_id);
        const x = (clip.x + clip.width / 2 - 0.5) * doc.width,
          y = (clip.y + clip.height / 2 - 0.5) * doc.height;
        return {
          id: sdkId(clip.id, "e"),
          trackId: sdkId(track.id, "t"),
          type: clip.kind,
          name:
            clip.kind === "text"
              ? clip.text.slice(0, 40)
              : source?.name || clip.kind,
          s: clip.start_us / 1e6,
          e: (clip.start_us + clip.duration_us) / 1e6,
          props: {
            src: source ? sdkMediaUrl(source.uri) : undefined,
            crispySourceId: clip.source_id,
            crispyClip: clip,
            time: clip.source_in_us / 1e6,
            playbackRate: 1,
            volume: clip.volume,
            opacity: clip.opacity,
            text: clip.text,
            fontSize: clip.font_size,
            fontFamily: "Arial",
            fill: clip.color,
            textAlign: "left",
            width: clip.width * doc.width,
            height: clip.height * doc.height,
            x,
            y,
          },
          frame: {
            x,
            y,
            size: [clip.width * doc.width, clip.height * doc.height],
          },
          objectFit: clip.fit,
          mediaDuration: (source?.duration_us || 0) / 1e6,
        };
      }),
    })),
  };
}

export function fromTwick(
  project: ProjectJSON,
  previous: VideoDocument,
  sources: VideoSource[],
): VideoDocument {
  const old = new Map(
    previous.tracks.flatMap((t) =>
      t.clips.map((c) => [sdkId(c.id, "e"), c] as const),
    ),
  );
  const oldTracks = new Map(previous.tracks.map((t) => [sdkId(t.id, "t"), t]));
  return {
    ...previous,
    tracks: project.tracks.map((track) => ({
      id: oldTracks.get(track.id)?.id || track.props?.crispyTrackId || track.id,
      name: track.name,
      clips: track.elements.map((element) => {
        const source = sources.find(
          (s) =>
            s.id === element.props?.crispySourceId ||
            sdkMediaUrl(s.uri) === element.props?.src ||
            videoMediaUrl(s.uri) === element.props?.src,
        );
        const kind = element.type as VideoClip["kind"];
        const snapshot = element.props?.crispyClip as VideoClip | undefined;
        const original =
          snapshot || old.get(element.id) || newClip(kind, source);
        const id =
          snapshot && sdkId(snapshot.id, "e") === element.id
            ? snapshot.id
            : old.get(element.id)?.id || element.id;
        return {
          ...original,
          id,
          kind,
          source_id: source?.id ?? null,
          start_us: Math.max(0, Math.round(element.s * 1e6)),
          duration_us: Math.max(1, Math.round((element.e - element.s) * 1e6)),
          source_in_us: Math.max(
            0,
            Math.round(Number(element.props?.time || 0) * 1e6),
          ),
          volume: Number(element.props?.volume ?? original.volume),
          opacity: Number(element.props?.opacity ?? original.opacity),
          text: String(element.props?.text || original.text),
          font_size: Number(element.props?.fontSize || original.font_size),
          color: String(element.props?.fill || original.color),
        };
      }),
    })),
  };
}
