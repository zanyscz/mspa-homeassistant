from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.const import PRECISION_HALVES
from .const import DOMAIN, TEMP_UNIT, MAX_TEMP, MIN_TEMP
from .entity import MSpaClimateEntity

# True on Home Assistant 2026.11 and later, where ClimateEntity reads `native_*`.
_NATIVE_TEMPERATURE_API = hasattr(ClimateEntity, "native_current_temperature")


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MSpaClimate(coordinator)])


class MSpaClimate(MSpaClimateEntity):
    """Representation of the MSpa climate control entity."""
    name = "Heater Control"

    _attr_precision = PRECISION_HALVES
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    # Home Assistant 2026.11 renamed the unit and the two readings to `native_*` and
    # deprecated the old names, which stop working in 2027.11. `hacs.json` still allows
    # 2026.3, where only the old names are read, so the class declares whichever set the
    # running Home Assistant understands. Declaring both would keep the warning alive:
    # it fires on the old names being *provided*, not on them being read.
    if _NATIVE_TEMPERATURE_API:
        _attr_native_temperature_unit = TEMP_UNIT
    else:
        _attr_temperature_unit = TEMP_UNIT

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_climate_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_icon = "mdi:hot-tub"
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
        self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
        )

    @property
    def entity_picture(self):
        """The spa's photograph, so a picture card has something to point at.

        It has to be `entity_picture` and not an attribute of our own naming — that is
        the key the picture cards read. Home Assistant shows it in preference to the
        icon, so this entity loses its mdi:hot-tub in the device panel; that is the
        trade, and it is why the property lives here and not on MSpaBaseEntity, where
        every sensor, switch and number would inherit it and the panel would be a column
        of identical photographs with nothing to tell the rows apart.

        The climate entity carries it because it is the spa itself — the one a card
        would naturally be pointed at.
        """
        return getattr(self.coordinator, "product_pic_url", None)

    def _water_temperature(self):
        return self.coordinator.last_data.get("water_temperature")

    def _target_temperature(self):
        return self.coordinator.last_data.get("target_temperature")

    if _NATIVE_TEMPERATURE_API:
        native_current_temperature = property(_water_temperature)
        native_target_temperature = property(_target_temperature)
    else:
        current_temperature = property(_water_temperature)
        target_temperature = property(_target_temperature)

    async def async_set_temperature(self, **kwargs):
        temperature = kwargs.get("temperature")
        if temperature is not None:
            await self.coordinator.set_temperature(type("ServiceCall", (), {"data": {"temperature": temperature}})())

    @property
    def hvac_mode(self):
        return HVACMode.HEAT if self.coordinator.last_data.get("heater") == "on" else HVACMode.OFF

    @property
    def hvac_action(self):
        heat_state = self.coordinator.last_data.get("heat_state")
        if heat_state == 2:
            return HVACAction.PREHEATING  # You may need to define this if not present
        if heat_state == 3:
            return HVACAction.HEATING
        if heat_state == 4:
            return HVACAction.IDLE
        return HVACAction.OFF

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode == HVACMode.HEAT:
            await self.coordinator.set_feature_state("heater", "on")
        elif hvac_mode == HVACMode.OFF:
            await self.coordinator.set_feature_state("heater", "off")
