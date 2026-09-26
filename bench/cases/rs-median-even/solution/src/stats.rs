/// A running list of response times, in milliseconds.
pub struct Latencies {
    samples: Vec<u32>,
}

impl Default for Latencies {
    fn default() -> Self {
        Self::new()
    }
}

impl Latencies {
    pub fn new() -> Self {
        Latencies { samples: Vec::new() }
    }

    pub fn record(&mut self, ms: u32) {
        self.samples.push(ms);
    }

    /// The median response time: the middle sample, or the mean of the two middle samples.
    pub fn median(&self) -> Option<f64> {
        if self.samples.is_empty() {
            return None;
        }
        let mut sorted = self.samples.clone();
        sorted.sort();
        let mid = sorted.len() / 2;
        if sorted.len() % 2 == 0 {
            Some((sorted[mid - 1] as f64 + sorted[mid] as f64) / 2.0)
        } else {
            Some(sorted[mid] as f64)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_samples() {
        assert_eq!(Latencies::new().median(), None);
    }

    #[test]
    fn odd_number_of_samples() {
        let mut latencies = Latencies::new();
        for ms in [30, 10, 20] {
            latencies.record(ms);
        }
        assert_eq!(latencies.median(), Some(20.0));
    }
}
