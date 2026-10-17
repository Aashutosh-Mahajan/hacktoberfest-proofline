import { useStore } from "../store/store";
import { VoiceWarRoom } from "../panels/VoiceWarRoom";

export default function AskView() {
  const voice = useStore((s) => s.voice);
  return (
    <main className="page">
      <section className="card">
        <h2>Ask ProofLine</h2>
        <p className="cap">Ask a question out loud or by typing, in English or Hindi. For example: "Why is BANK_X failing?"</p>
        <VoiceWarRoom items={voice} />
      </section>
    </main>
  );
}
