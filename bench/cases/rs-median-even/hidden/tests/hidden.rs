use latency::stats::Latencies;

#[test]
fn an_even_count_averages_the_middle_two() {
    let mut latencies = Latencies::new();
    for ms in [40, 10, 30, 20] {
        latencies.record(ms);
    }
    assert_eq!(latencies.median(), Some(25.0));
}

#[test]
fn two_samples() {
    let mut latencies = Latencies::new();
    latencies.record(1);
    latencies.record(2);
    assert_eq!(latencies.median(), Some(1.5));
}

#[test]
fn an_odd_count_still_takes_the_middle() {
    let mut latencies = Latencies::new();
    for ms in [5, 1, 3] {
        latencies.record(ms);
    }
    assert_eq!(latencies.median(), Some(3.0));
}
