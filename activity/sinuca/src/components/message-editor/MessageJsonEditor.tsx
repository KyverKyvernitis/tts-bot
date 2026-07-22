interface MessageJsonEditorProps {
  value: string;
  error: string | null;
  dirty: boolean;
  onChange(value: string): void;
}

export function MessageJsonEditor({ value, error, dirty, onChange }: MessageJsonEditorProps) {
  return (
    <div className="osk-message-json">
      <div className="osk-message-json__head">
        <div>
          <strong>Objeto plano dos campos</strong>
          <small>As chaves são os IDs completos dos campos. Pontos não criam objetos aninhados.</small>
        </div>
        {dirty && <span className="osk-badge" data-state="changed">não aplicado</span>}
      </div>
      <textarea
        className="osk-message-json__textarea"
        value={value}
        spellCheck={false}
        aria-invalid={Boolean(error)}
        onChange={(event) => onChange(event.target.value)}
      />
      {error && <p className="osk-message-json__error">{error}</p>}
    </div>
  );
}
