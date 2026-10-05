import { Component, type ReactNode } from "react";

/** Shows a message instead of a blank window if the UI itself crashes. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="token-prompt">
        <form onSubmit={(e) => e.preventDefault()}>
          <h1>Something went wrong</h1>
          <p>{this.state.error.message}</p>
          <button className="primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </form>
      </div>
    );
  }
}
