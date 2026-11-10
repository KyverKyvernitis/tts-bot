//! Pending speech queue with the bot's existing whole-message eviction policy.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};

#[derive(Clone, Deserialize, Serialize)]
pub struct Entry {
    pub token: u64,
    pub group: String,
    pub signature: String,
}
pub struct PendingQueue {
    maxsize: usize,
    generation: u64,
    accepting: bool,
    items: VecDeque<Entry>,
    signatures: HashMap<String, usize>,
}
impl PendingQueue {
    pub fn new(maxsize: usize) -> Result<Self, String> {
        if maxsize == 0 {
            return Err("native pending queue needs a positive capacity".into());
        }
        Ok(Self {
            maxsize,
            generation: 0,
            accepting: true,
            items: VecDeque::new(),
            signatures: HashMap::new(),
        })
    }
    fn increment(&mut self, signature: &str) {
        *self.signatures.entry(signature.into()).or_default() += 1;
    }
    fn decrement(&mut self, signature: &str) {
        if let Some(count) = self.signatures.get_mut(signature) {
            *count = count.saturating_sub(1);
            if *count == 0 {
                self.signatures.remove(signature);
            }
        }
    }
    fn push(&mut self, entry: Entry) {
        self.increment(&entry.signature);
        self.items.push_back(entry);
    }
    pub fn pop(&mut self) -> Option<u64> {
        let item = self.items.pop_front()?;
        self.decrement(&item.signature);
        Some(item.token)
    }
    pub fn put(&mut self, entry: Entry) -> Result<(), String> {
        if self.items.len() >= self.maxsize {
            return Err("queue full".into());
        }
        if self.items.iter().any(|e| e.token == entry.token) {
            return Err("duplicate item token".into());
        }
        self.push(entry);
        Ok(())
    }
    pub fn admit(&mut self, entries: Vec<Entry>) -> Result<Value, String> {
        if !self.accepting || entries.is_empty() {
            return Ok(
                json!({"accepted":false,"dropped":[],"dropped_count":0,"deduplicated":false,"generation":self.generation,"depths":[]}),
            );
        }
        let mut unique = HashSet::new();
        for entry in &entries {
            if !unique.insert(entry.token) || self.items.iter().any(|e| e.token == entry.token) {
                return Err("duplicate item token".into());
            }
        }
        if entries
            .iter()
            .all(|e| self.signatures.get(&e.signature).copied().unwrap_or(0) > 0)
        {
            return Ok(
                json!({"accepted":false,"dropped":[],"dropped_count":0,"deduplicated":true,"generation":self.generation,"depths":[]}),
            );
        }
        if entries.len() > self.maxsize {
            return Ok(
                json!({"accepted":false,"dropped":[],"dropped_count":entries.len(),"deduplicated":false,"generation":self.generation,"depths":[]}),
            );
        }
        let mut dropped = Vec::new();
        while self.items.len() + entries.len() > self.maxsize {
            let Some(first) = self.items.front().cloned() else {
                break;
            };
            dropped.push(self.pop().unwrap());
            while self.items.front().is_some_and(|e| e.group == first.group) {
                dropped.push(self.pop().unwrap());
            }
        }
        let mut depths = Vec::new();
        for entry in entries {
            self.push(entry);
            depths.push(self.items.len());
        }
        Ok(
            json!({"accepted":true,"dropped_count":dropped.len(),"dropped":dropped,"deduplicated":false,"generation":self.generation,"depths":depths}),
        )
    }
    pub fn len(&self) -> usize {
        self.items.len()
    }
    pub fn tokens(&self) -> Vec<u64> {
        self.items.iter().map(|e| e.token).collect()
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn set_generation(&mut self, value: u64) -> Result<(), String> {
        if value < self.generation {
            return Err("queue generation cannot move backwards".into());
        }
        self.generation = value;
        Ok(())
    }
    pub fn accepting(&self) -> bool {
        self.accepting
    }
    pub fn set_accepting(&mut self, value: bool) {
        self.accepting = value;
    }
    pub fn signatures(&self) -> Value {
        json!(self.signatures)
    }
    pub fn clear_signatures(&mut self) {
        self.signatures.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn e(token: u64, group: &str, signature: &str) -> Entry {
        Entry {
            token,
            group: group.into(),
            signature: signature.into(),
        }
    }
    #[test]
    fn shared_request_id_parts_evict_together() {
        let mut q = PendingQueue::new(3).unwrap();
        assert_eq!(
            q.admit(vec![
                e(1, "same-request", "part-a"),
                e(2, "same-request", "part-b"),
                e(3, "other", "part-c")
            ])
            .unwrap()["accepted"],
            true
        );
        let admission = q
            .admit(vec![e(4, "new", "new-a"), e(5, "new", "new-b")])
            .unwrap();
        assert_eq!(admission["dropped"], json!([1, 2]));
        assert_eq!(q.tokens(), vec![3, 4, 5]);
        assert_eq!(q.signatures()["part-a"], Value::Null);
    }
    #[test]
    fn duplicate_never_evicts_and_failure_is_atomic() {
        let mut q = PendingQueue::new(2).unwrap();
        q.admit(vec![e(1, "a", "same"), e(2, "b", "other")])
            .unwrap();
        assert_eq!(
            q.admit(vec![e(3, "c", "same")]).unwrap()["deduplicated"],
            true
        );
        assert_eq!(q.tokens(), vec![1, 2]);
        assert_eq!(
            q.admit(vec![e(3, "new", "1"), e(4, "new", "2"), e(5, "new", "3")])
                .unwrap()["accepted"],
            false
        );
        assert_eq!(q.tokens(), vec![1, 2]);
        assert_eq!(q.pop(), Some(1));
        assert_eq!(q.signatures()["same"], Value::Null);
        q.set_generation(1).unwrap();
        assert!(q.set_generation(0).is_err());
    }
}
