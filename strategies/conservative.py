from strategies.base_strategy import LawvisoryBaseStrategy

class ConservativeStrategy(LawvisoryBaseStrategy):
    def initialize(self):
        super().initialize(risk_profile_name="conservative")