/**
 * Commander — full-page operations assistant. Same component and endpoint as
 * the Command Centre sidebar; every number in an answer is traced to a tool
 * result by the backend's grounding validator.
 */
import CommanderBar, { ALL_QUESTIONS } from '../components/CommanderBar.jsx';
import PageShell from '../components/PageShell.jsx';

export default function Commander() {
  return (
    <PageShell
      title="Commander"
      subtitle="Ask about the live city in plain language. Answers come from tool calls against live state, the look-ahead projection and the what-if simulator; open Sources on any answer to see them."
    >
      <div className="h-[calc(100vh-190px)] min-h-[420px]">
        <CommanderBar questions={ALL_QUESTIONS} />
      </div>
    </PageShell>
  );
}
