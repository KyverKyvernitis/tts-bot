export function runSingleFlight<K, V>(
  flights: Map<K, Promise<V>>,
  key: K,
  factory: () => Promise<V>,
): Promise<V> {
  const existing = flights.get(key);
  if (existing) return existing;

  const flight = Promise.resolve().then(factory);
  flights.set(key, flight);
  const clear = () => {
    if (flights.get(key) === flight) flights.delete(key);
  };
  void flight.then(clear, clear);
  return flight;
}
