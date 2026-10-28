import { useEffect, useRef, useState } from "react";

import { useDecision } from "../api/hooks";
import { useToast } from "./Shell";

const KEY = "pilot.approver";

function storedName(): string {
  try {
    return window.localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

interface Props {
  runId: string;
  decision: "approved" | "rejected" | null;
  prLabel: string;
  onClose: () => void;
}

/** Approve or reject. The approver's name is recorded with the decision (SSO replaces the field later). */
export function DecisionDialog({ runId, decision, prLabel, onClose }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(storedName);
  const [comment, setComment] = useState("");
  const mutation = useDecision(runId);
  const toast = useToast();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (decision && !dialog.open) dialog.showModal();
    if (!decision && dialog.open) dialog.close();
  }, [decision]);

  const approving = decision === "approved";
  const confirm = () => {
    if (!decision || !name.trim()) return;
    try {
      window.localStorage.setItem(KEY, name.trim());
    } catch {
      /* storage can be blocked; the name is still sent */
    }
    mutation.mutate(
      { decision, decided_by: name.trim(), comment: comment.trim() || null },
      {
        onSuccess: () => {
          toast(approving ? "Approved. Pilot is opening the pull request." : "Rejected. No pull request will be opened.");
          onClose();
        },
        onError: (error: Error) => toast(error.message),
      },
    );
  };

  return (
    <dialog ref={ref} aria-labelledby="decision-title" onClose={onClose}>
      <h2 id="decision-title">{approving ? "Approve and open a pull request?" : "Reject this upgrade?"}</h2>
      <p className="small muted">
        {approving
          ? `Pilot opens ${prLabel} with the upgrade, the code changes and the evidence. Your normal review and CI still apply. Nothing is merged automatically.`
          : "Pilot keeps the analysis but opens nothing. You can run the scan again later."}
      </p>
      <label htmlFor="approver">Your name</label>
      <input id="approver" type="text" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
      <label htmlFor="note">Note for reviewers (optional)</label>
      <textarea id="note" value={comment} onChange={(e) => setComment(e.target.value)} />
      <div className="actions" style={{ marginTop: 14 }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" onClick={confirm} disabled={!name.trim() || mutation.isPending}>
          {approving ? "Approve and open PR" : "Reject"}
        </button>
      </div>
    </dialog>
  );
}
