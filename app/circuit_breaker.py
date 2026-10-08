import time


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 2,
        recovery_timeout: float = 5.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self.failure_count = 0
        self.state = "CLOSED"
        self.opened_at = None

    def allow_request(self) -> bool:
        """
        判断当前请求是否允许访问后端服务。
        """

        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":
            elapsed = time.time() - self.opened_at

            if elapsed >= self.recovery_timeout:
                self.state = "HALF_OPEN"
                return True

            return False

        if self.state == "HALF_OPEN":
            return True

        return False

    def record_success(self):
        """
        调用成功：
        服务认为已经恢复，重新 CLOSED。
        """

        self.failure_count = 0
        self.state = "CLOSED"
        self.opened_at = None

    def record_failure(self):
        """
        一次完整 Tool 调用最终失败。
        """

        self.failure_count += 1

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            self.opened_at = time.time()