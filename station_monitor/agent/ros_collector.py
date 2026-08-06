import threading
from typing import Any


class RosCollector:
    """ROS2 collector. It stays optional so the agent remains diagnosable without ROS."""

    def __init__(self, joint_topic: str, temperature_topics: list[str], ros_domain_id: int = 0):
        self.joint_topic = joint_topic
        self.temperature_topics = temperature_topics
        self.ros_domain_id = ros_domain_id
        self.joints: dict[str, float] = {}
        self.temperatures: dict[str, float] = {}
        self.error: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ros-collector", daemon=True)
        self._thread.start()

    def snapshot(self) -> tuple[dict[str, float], dict[str, float]]:
        return dict(self.joints), dict(self.temperatures)

    def _run(self) -> None:
        import os
        os.environ["ROS_DOMAIN_ID"] = str(self.ros_domain_id)
        try:
            import rclpy
            from diagnostic_msgs.msg import DiagnosticArray
            from sensor_msgs.msg import JointState, Temperature
        except ImportError as exc:
            self.error = f"ROS2 Python 组件不可用: {exc}"
            return
        try:
            rclpy.init(args=None)
            node = rclpy.create_node("station_monitor_agent")

            def joint_callback(msg: Any) -> None:
                names = list(msg.name) or [f"joint_{i + 1}" for i in range(len(msg.position))]
                self.joints = {name: float(value) for name, value in zip(names, msg.position)}

            node.create_subscription(JointState, self.joint_topic, joint_callback, 10)
            for spec in self.temperature_topics:
                # Syntax: topic or topic|diagnostic. Plain topics use sensor_msgs/Temperature.
                topic, _, kind = spec.partition("|")
                if kind == "diagnostic":
                    node.create_subscription(
                        DiagnosticArray, topic,
                        lambda msg: self._diagnostic_temperature(msg), 10,
                    )
                else:
                    name = topic.strip("/").replace("/", "_")
                    node.create_subscription(
                        Temperature, topic,
                        lambda msg, key=name: self.temperatures.__setitem__(key, float(msg.temperature)), 10,
                    )
            rclpy.spin(node)
        except Exception as exc:
            self.error = str(exc)

    def _diagnostic_temperature(self, msg: Any) -> None:
        values: dict[str, float] = {}
        for status in msg.status:
            for pair in status.values:
                if "temp" in pair.key.lower():
                    try:
                        values[f"{status.name}:{pair.key}"] = float(pair.value)
                    except ValueError:
                        continue
        if values:
            self.temperatures.update(values)
