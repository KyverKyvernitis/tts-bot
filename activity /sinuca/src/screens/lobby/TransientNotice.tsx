type TransientNoticeProps = {
  message: string | null;
};

export default function TransientNotice({ message }: TransientNoticeProps) {
  if (!message) return null;

  return (
    <div className="activity-notice activity-notice--visible" role="status" aria-live="polite">
      <div className="activity-notice__panel">{message}</div>
    </div>
  );
}
