package com.shop;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class PricingTest {
    @Test
    void noDiscount() {
        assertEquals(1000, Pricing.applyDiscount(1000, 0));
    }

    @Test
    void everythingFree() {
        assertEquals(0, Pricing.applyDiscount(1000, 100));
    }
}
