package com.shop;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class PricingHiddenTest {
    @Test
    void tenPercentOff() {
        assertEquals(900, Pricing.applyDiscount(1000, 10));
    }

    @Test
    void theDiscountRoundsDown() {
        assertEquals(900, Pricing.applyDiscount(999, 10));
    }

    @Test
    void cartTotal() {
        assertEquals(1350, Pricing.cartTotal(new int[] {1000, 500}, 10));
    }
}
