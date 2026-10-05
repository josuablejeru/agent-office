import { type FormEvent, useState } from "react";

export function TokenPrompt({ onSubmit }: { onSubmit: (token: string) => Promise<void> }) {
  const [value, setValue] = useState("");

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void onSubmit(value.trim());
  }

  return (
    <div className="token-prompt">
      <form onSubmit={handleSubmit}>
        <h1>Agent Office</h1>
        <p>
          Open the link printed by <code>scripts/start-dev.sh</code>, or paste the API token from{" "}
          <code>~/.config/agent-office/secrets/api-token</code>.
        </p>
        <input
          autoFocus
          type="password"
          placeholder="API token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button className="primary" type="submit" disabled={!value.trim()}>
          Continue
        </button>
      </form>
    </div>
  );
}
