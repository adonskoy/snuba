use crate::types::{Message, Partition};
use std::collections::HashMap;
use std::time::Duration;

pub mod commit_offsets;
pub mod produce;
pub mod reduce;
pub mod run_task_in_threads;
pub mod transform;

#[derive(Debug, Clone)]
pub enum SubmitError<T> {
    MessageRejected(MessageRejected<T>),
    InvalidMessage(InvalidMessage),
}

#[derive(Debug, Clone)]
pub struct MessageRejected<T> {
    pub message: Message<T>,
}

#[derive(Debug, Clone)]
pub struct InvalidMessage {
    pub partition: Partition,
    pub offset: u64,
}

/// Signals that we need to commit offsets
#[derive(Debug, Clone, PartialEq)]
pub struct CommitRequest {
    pub positions: HashMap<Partition, u64>,
}

impl CommitRequest {
    pub fn merge(mut self, other: CommitRequest) -> Self {
        // Merge commit requests, keeping the highest offset for each partition
        for (partition, offset) in other.positions {
            if self.positions.contains_key(&partition) {
                if self.positions[&partition] < offset {
                    self.positions.insert(partition, offset);
                }
            } else {
                self.positions.insert(partition, offset);
            }
        }

        self
    }
}

pub fn merge_commit_request(
    value: Option<CommitRequest>,
    other: Option<CommitRequest>,
) -> Option<CommitRequest> {
    match (value, other) {
        (None, None) => None,
        (Some(x), None) => Some(x),
        (None, Some(y)) => Some(y),
        (Some(x), Some(y)) => Some(x.merge(y)),
    }
}

/// A processing strategy defines how a stream processor processes messages
/// during the course of a single assignment. The processor is instantiated
/// when the assignment is received, and closed when the assignment is
/// revoked.
///
/// This interface is intentionally not prescriptive, and affords a
/// significant degree of flexibility for the various implementations.
pub trait ProcessingStrategy<TPayload: Clone>: Send + Sync {
    /// Poll the processor to check on the status of asynchronous tasks or
    /// perform other scheduled work.
    ///
    /// This method is called on each consumer loop iteration, so this method
    /// should not be used to perform work that may block for a significant
    /// amount of time and block the progress of the consumer or exceed the
    /// consumer poll interval timeout.
    ///
    /// This method may raise exceptions that were thrown by asynchronous
    /// tasks since the previous call to ``poll``.
    fn poll(&mut self) -> Result<Option<CommitRequest>, InvalidMessage>;

    /// Submit a message for processing.
    ///
    /// Messages may be processed synchronously or asynchronously, depending
    /// on the implementation of the processing strategy. Callers of this
    /// method should not assume that this method returning successfully
    /// implies that the message was successfully processed.
    ///
    /// If the processing strategy is unable to accept a message (due to it
    /// being at or over capacity, for example), this method will raise a
    /// ``MessageRejected`` exception.
    fn submit(&mut self, message: Message<TPayload>) -> Result<(), SubmitError<TPayload>>;

    /// Close this instance. No more messages should be accepted by the
    /// instance after this method has been called.
    ///
    /// This method should not block. Once this strategy instance has
    /// finished processing (or discarded) all messages that were submitted
    /// prior to this method being called, the strategy should commit its
    /// partition offsets and release any resources that will no longer be
    /// used (threads, processes, sockets, files, etc.)
    fn close(&mut self);

    /// Close the processing strategy immediately, abandoning any work in
    /// progress. No more messages should be accepted by the instance after
    /// this method has been called.
    fn terminate(&mut self);

    /// Block until the processing strategy has completed all previously
    /// submitted work, or the provided timeout has been reached. This method
    /// should be called after ``close`` to provide a graceful shutdown.
    ///
    /// This method is called synchronously by the stream processor during
    /// assignment revocation, and blocks the assignment from being released
    /// until this function exits, allowing any work in progress to be
    /// completed and committed before the continuing the rebalancing
    /// process.
    fn join(&mut self, timeout: Option<Duration>) -> Result<Option<CommitRequest>, InvalidMessage>;
}

pub trait ProcessingStrategyFactory<TPayload: Clone>: Send + Sync {
    /// Instantiate and return a ``ProcessingStrategy`` instance.
    ///
    /// :param commit: A function that accepts a mapping of ``Partition``
    /// instances to offset values that should be committed.
    fn create(&self) -> Box<dyn ProcessingStrategy<TPayload>>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Topic;

    #[test]
    fn merge() {
        let partition = Partition {
            topic: Topic {
                name: "topic".to_string(),
            },
            index: 0,
        };
        let partition_2 = Partition {
            topic: Topic {
                name: "topic".to_string(),
            },
            index: 1,
        };

        let a = Some(CommitRequest {
            positions: HashMap::from([(partition.clone(), 1)]),
        });

        let b = Some(CommitRequest {
            positions: HashMap::from([(partition.clone(), 2)]),
        });

        let c = Some(CommitRequest {
            positions: HashMap::from([(partition_2.clone(), 2)]),
        });

        assert_eq!(merge_commit_request(a.clone(), b.clone()), b.clone());

        assert_eq!(
            merge_commit_request(a.clone(), c.clone()),
            Some(CommitRequest {
                positions: HashMap::from([(partition.clone(), 1), (partition_2.clone(), 2)]),
            })
        );

        assert_eq!(merge_commit_request(c.clone(), None), c.clone());
    }
}
