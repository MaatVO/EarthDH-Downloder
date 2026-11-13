// NiceGUI's select.js with a search that suits long catalogues (SearchSelect in app.py).
// Typing filters the options: every word must appear in an option's label or in its search text, in any order,
// ignoring case, accents and punctuation ("era5 land" finds "ERA5-Land"). Options whose label holds every word
// come first. Opening the popup selects the field's text, so typing replaces the chosen option's name.
const fold = (s) =>
  String(s ?? "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();

export default {
  props: ["options", "search"],
  template: `
    <q-select
      ref="qRef"
      :options="filteredOptions"
      @filter="filterFn"
      @popup-show="onShow"
      @popup-hide="removeClass"
      @input-value="forwardInputValue"
      @update:model-value="forwardModelValue"
    >
      <template v-for="(_, slot) in $slots" v-slot:[slot]="slotProps">
        <slot :name="slot" v-bind="slotProps || {}" />
      </template>
    </q-select>
  `,
  emits: ["input-value", "update:model-value"],
  data() {
    return {
      initialOptions: this.options,
      filteredOptions: this.options,
      modelValueChanged: false,
    };
  },
  computed: {
    // the folded label and label + search text of each option, by option value (NiceGUI numbers options)
    haystacks() {
      const extra = this.search || [];
      return (this.options || []).map((o) => {
        const label = fold(o.label);
        return { label, all: `${label} ${fold(extra[o.value])}` };
      });
    },
  },
  methods: {
    forwardModelValue(...args) {
      this.modelValueChanged = true;
      this.$emit("update:model-value", ...args);
    },
    async forwardInputValue(value) {
      this.modelValueChanged = false;
      await this.$nextTick(); // wait for a possible "update:model-value" event indicating a selection
      if (!this.modelValueChanged) {
        this.$emit("input-value", value); // suppress selection-induced events which would re-trigger filtering
      }
    },
    // the label of the chosen option. NiceGUI numbers the options: the model value is {value: index, label}, and
    // the label is looked up from the options by that value (the value may also arrive as a bare index)
    chosenLabel() {
      const mv = this.$attrs["model-value"] ?? this.$attrs.modelValue;
      if (mv == null) return null;
      const index = typeof mv === "object" ? mv.value : mv;
      const opt = (this.options || []).find((o) => o.value === index);
      return opt ? opt.label : (typeof mv === "object" ? mv.label ?? null : null);
    },
    filterFn(val, update) {
      // the field still shows the chosen option's name when the popup opens: that lists every option
      const chosen = this.chosenLabel();
      const needle = chosen != null && val === chosen ? "" : val;
      update(() => (this.filteredOptions = this.findFilteredOptions(needle)));
    },
    findFilteredOptions(val) {
      const words = fold(val).split(" ").filter(Boolean);
      if (!words.length) return this.initialOptions;
      const inLabel = [];
      const inSearch = [];
      for (const o of this.initialOptions) {
        const h = this.haystacks[o.value];
        if (!h || !words.every((w) => h.all.includes(w))) continue;
        (words.every((w) => h.label.includes(w)) ? inLabel : inSearch).push(o);
      }
      return inLabel.concat(inSearch);
    },
    onShow() {
      // prevent the page from scrolling when the select popup is closed (NiceGUI #5031)
      document.documentElement.classList.add("nicegui-select-popup-open");
      this.$el.querySelector("input")?.select();
    },
    async removeClass() {
      await this.$nextTick();
      document.documentElement.classList.remove("nicegui-select-popup-open");
    },
  },
  unmounted() {
    this.removeClass();
  },
  watch: {
    options: {
      handler(newOptions) {
        this.initialOptions = newOptions;
        this.filteredOptions = newOptions;
      },
      immediate: true,
    },
  },
};
