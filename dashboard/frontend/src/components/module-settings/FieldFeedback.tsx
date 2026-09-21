import { createContext, useContext } from "react";

interface FieldFeedback { errors: Record<string, string>; optionsBusy?: boolean; onRetryOptions?(): void }
export const FieldFeedbackContext = createContext<FieldFeedback>({ errors: {} });
export const useFieldFeedback = () => useContext(FieldFeedbackContext);
