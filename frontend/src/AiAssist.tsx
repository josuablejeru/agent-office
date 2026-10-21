import { useEffect, useState } from "react";
import { api } from "./api";

/** Turns a sanitized SVG drawing into a PNG file, the format avatar uploads take. */
export async function svgToPng(svg: string, size = 256): Promise<File> {
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  try {
    const image = new Image();
    image.width = size;
    image.height = size;
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("The drawing could not be read."));
      image.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    canvas.getContext("2d")!.drawImage(image, 0, 0, size, size);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("The drawing could not be turned into a picture.");
    return new File([blob], "avatar.png", { type: "image/png" });
  } finally {
    URL.revokeObjectURL(url);
  }
}

interface ModelChoice {
  provider: string;
  model: string;
}

/** "Write with AI": a short role in, a job description out (shown for editing, not saved). */
export function WriteWithAi({
  choice,
  name,
  onWritten,
}: {
  choice: ModelChoice;
  name: string;
  onWritten: (text: string) => void;
}) {
  const [role, setRole] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function write() {
    setBusy(true);
    setError(null);
    try {
      const { text } = await api.writeJobDescription({ ...choice, name, role });
      onWritten(text);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ai-assist">
      <div className="ai-assist-row">
        <input
          placeholder="Describe the role, e.g. “handles supplier invoices and chases late payments”"
          value={role}
          onChange={(e) => setRole(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              if (role.trim().length >= 3 && choice.model) void write();
            }
          }}
        />
        <button
          type="button"
          disabled={busy || role.trim().length < 3 || !choice.model}
          title={choice.model ? `Uses ${choice.model}` : "Choose a model first"}
          onClick={() => void write()}
        >
          {busy ? "Writing…" : "Write with AI"}
        </button>
      </div>
      {!choice.model && <p className="hint">Choose a model above to write with AI.</p>}
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}

/** "Draw with AI": a few avatar drawings to choose from; the chosen one becomes the agent's picture. */
export function DrawWithAi({
  choice,
  suggestion,
  onChosen,
  onClose,
}: {
  choice: ModelChoice;
  suggestion: string;
  onChosen: (picture: File) => void;
  onClose: () => void;
}) {
  const [description, setDescription] = useState(suggestion);
  const [drawings, setDrawings] = useState<string[]>([]);
  const [urls, setUrls] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const made = drawings.map((svg) => URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" })));
    setUrls(made);
    return () => made.forEach((url) => URL.revokeObjectURL(url));
  }, [drawings]);

  async function draw() {
    setBusy(true);
    setError(null);
    try {
      const { candidates } = await api.drawAvatars({ ...choice, description, count: 3 });
      setDrawings(candidates);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function choose(svg: string) {
    try {
      onChosen(await svgToPng(svg));
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="ai-draw">
      <div className="ai-assist-row">
        <input
          placeholder="What should the picture show? e.g. “friendly accountant with glasses, green shirt”"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <button
          type="button"
          className="primary"
          disabled={busy || description.trim().length < 3 || !choice.model}
          onClick={() => void draw()}
        >
          {busy ? "Drawing…" : drawings.length ? "Try again" : "Draw"}
        </button>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </div>
      {busy && <p className="hint">Drawing three versions with {choice.model}; this can take a minute.</p>}
      {error && <p className="form-error">{error}</p>}
      {urls.length > 0 && (
        <div className="ai-draw-choices">
          {urls.map((url, index) => (
            <button type="button" key={url} className="ai-draw-choice" onClick={() => void choose(drawings[index])}>
              <img src={url} alt={`Drawing ${index + 1}`} />
              <span>Use this one</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
