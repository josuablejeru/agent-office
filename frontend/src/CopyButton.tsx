import { useState } from "react";

/** Copies text to the clipboard, falling back to the older route where the clipboard API is not allowed. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    return copied;
  }
}

/** A small "Copy" button; shows "Copied" for a moment after it worked. */
export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  async function copy() {
    const copied = await copyText(text);
    setState(copied ? "copied" : "failed");
    window.setTimeout(() => setState("idle"), 1500);
  }

  return (
    <button type="button" className="copy-button" title="Copy to the clipboard" onClick={() => void copy()}>
      {state === "copied" ? "Copied" : state === "failed" ? "Could not copy" : label}
    </button>
  );
}
